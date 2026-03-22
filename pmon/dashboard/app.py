"""FastAPI dashboard with auth and per-user data."""

from __future__ import annotations

import asyncio
import io
import logging
import os
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
    disable_totp, decode_token, create_initial_admin, create_otp_token,
)
from pmon.config import detect_retailer

if TYPE_CHECKING:
    from pmon.engine import PmonEngine

logger = logging.getLogger(__name__)


def _fix_utc_timestamps(row: dict, *fields: str) -> None:
    """Append 'Z' to SQLite UTC timestamps so browsers interpret them correctly."""
    for f in fields:
        val = row.get(f)
        if val and not val.endswith(("Z", "+00:00")):
            row[f] = val.replace(" ", "T") + "Z"


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

    # --- Health check (unauthenticated, for Docker/Watchtower) ---

    @app.get("/api/health")
    async def api_health():
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
        for c in checkouts:
            _fix_utc_timestamps(c, "created_at")

        # Check for pending OTP request
        pending_otp = db.get_pending_otp(user_id)
        if pending_otp:
            _fix_utc_timestamps(pending_otp, "created_at")

        total_spent = db.get_user_total_spent(user_id)
        settings = db.get_user_settings(user_id)

        return {
            "is_running": engine.state.is_running,
            "started_at": engine.state.started_at.isoformat() if engine.state.started_at else None,
            "products": product_list,
            "checkouts": checkouts,
            "pending_otp": pending_otp,
            "total_spent": total_spent,
            "spend_limit": settings.get("spend_limit", 0),
        }

    @app.post("/api/search")
    async def api_search(request: Request, user: dict = Depends(get_current_user)):
        """Search Target's RedSky API by keyword and return matching products."""
        from pmon.monitors.redsky_poller import RedSkySearch
        data = await request.json()
        keyword = data.get("keyword", "").strip()
        if not keyword:
            return JSONResponse({"error": "Keyword required"}, 400)
        max_results = min(int(data.get("max_results", 10)), 20)
        sold_by_target_only = bool(data.get("sold_by_target_only", False))
        include_out_of_stock = bool(data.get("include_out_of_stock", False))
        search = RedSkySearch(max_results=max_results)
        try:
            results = await search.find(
                keyword,
                sold_by_target_only=sold_by_target_only,
                include_out_of_stock=include_out_of_stock,
            )
        except Exception as e:
            logger.error("Search failed for '%s': %s", keyword, e)
            return JSONResponse({"error": f"Search failed: {e}"}, 500)
        return {
            "ok": True,
            "keyword": keyword,
            "results": [
                {
                    "tcin": r.tcin,
                    "title": r.title,
                    "price": r.price,
                    "url": r.url,
                    "image_url": r.image_url,
                    "availability_status": r.availability_status,
                    "is_purchasable": r.is_purchasable,
                    "sold_by": r.sold_by,
                    "street_date": r.street_date,
                    "release_label": r.release_label,
                }
                for r in results
            ],
        }

    @app.post("/api/products")
    async def api_add_product(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        url = data.get("url", "").strip()
        if not url:
            return JSONResponse({"error": "URL required"}, 400)

        name = data.get("name", "")
        retailer = detect_retailer(url)
        if retailer == "unknown":
            return JSONResponse({"error": "Unsupported retailer. Supported: Pokemon Center, Target, Best Buy, Walmart"}, 400)
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

    @app.post("/api/products/test_cart")
    async def api_test_cart(request: Request, user: dict = Depends(get_current_user)):
        """Dry-run checkout: adds to cart and goes through checkout flow but stops
        before placing the order. Returns the actual result (pass/fail)."""
        data = await request.json()
        url = data["url"]
        products = db.get_user_products(user["id"])
        product = next((p for p in products if p["url"] == url), None)
        if not product:
            return JSONResponse({"error": "Product not found"}, 404)

        from pmon.config import Product
        from pmon.models import CheckoutStatus
        p = Product(url=url, name=product["name"], auto_checkout=True)
        try:
            result = await engine.manual_checkout(p, user_id=user["id"], dry_run=True)
        except Exception as e:
            logger.error("Test cart failed for %s: %s", url, e)
            return {"ok": False, "message": f"Test cart failed: {e}"}

        if result and result.status == CheckoutStatus.SUCCESS:
            return {"ok": True, "message": f"Test cart passed — dry run completed for {product['name']}"}
        elif result and result.error_message:
            return {"ok": False, "message": f"Test cart failed: {result.error_message}"}
        else:
            status = result.status.value if result else "unknown"
            return {"ok": False, "message": f"Test cart finished with status: {status}"}

    # --- Retailer accounts ---

    @app.get("/api/accounts")
    async def api_get_accounts(user: dict = Depends(get_current_user)):
        accounts = db.get_retailer_accounts(user["id"])
        # Don't send passwords back
        safe = {}
        for retailer, acc in accounts.items():
            safe[retailer] = {
                "email": acc["email"],
                "has_password": bool(acc["password"]),
                "has_cvv": bool(acc.get("card_cvv")),
                "has_phone_last4": bool(acc.get("phone_last4")),
                "has_account_last_name": bool(acc.get("account_last_name")),
            }
        return {"accounts": safe}

    @app.post("/api/accounts")
    async def api_set_account(request: Request, user: dict = Depends(get_current_user)):
        data = await request.json()
        retailer = data.get("retailer", "").strip()
        email = data.get("email", "").strip()
        password = data.get("password", "")
        card_cvv = data.get("card_cvv", "").strip()
        phone_last4 = data.get("phone_last4", "").strip()
        account_last_name = data.get("account_last_name", "").strip()
        if retailer not in ("target", "walmart", "bestbuy", "pokemoncenter", "costco"):
            return JSONResponse({"error": "Invalid retailer"}, 400)
        db.set_retailer_account(user["id"], retailer, email, password, card_cvv=card_cvv,
                                phone_last4=phone_last4, account_last_name=account_last_name)
        return {"ok": True}

    @app.post("/api/accounts/test")
    async def api_test_account(request: Request, user: dict = Depends(get_current_user)):
        """Test retailer login credentials using Playwright browser automation."""
        import base64
        import json
        import os

        data = await request.json()
        retailer = data.get("retailer", "").strip()
        if retailer not in ("target", "walmart", "bestbuy", "pokemoncenter", "costco"):
            return JSONResponse({"error": "Invalid retailer"}, 400)

        accounts = db.get_retailer_accounts(user["id"])
        acc = accounts.get(retailer)
        if not acc or not acc.get("email") or not acc.get("password"):
            return JSONResponse({"error": "No credentials saved for this retailer"}, 400)

        email = acc["email"]
        password = acc["password"]

        # --- Walmart: browser login is blocked by PerimeterX press-and-hold CAPTCHA.
        # Instead, validate that imported session cookies work via API call.
        if retailer == "walmart":
            return await _test_walmart_session(user)

        # --- Costco: Akamai blocks browser login; validate session cookies first,
        # fall back to browser login if no cookies are imported.
        if retailer == "costco":
            return await _test_costco_session(user, email, password)

        # --- All other retailers: use Playwright browser automation ---
        return await _test_login_browser(retailer, email, password, user)

    async def _test_walmart_session(user: dict):
        """Test Walmart account by validating imported session cookies via API.

        Walmart uses aggressive PerimeterX bot protection with a press-and-hold
        CAPTCHA that cannot be solved by headless browsers.  Instead of attempting
        browser login (which always fails), we validate that the user's imported
        session cookies are still valid by making an API call to Walmart's
        lightweight config endpoint.

        This function coordinates with the shared WalmartMonitor rate-limit
        state so the dashboard and monitor loop don't pile requests on top of
        each other.
        """
        import json as _json
        import httpx
        from pmon.monitors.base import DEFAULT_HEADERS

        # Check if the Walmart monitor is currently in a rate-limit cooldown.
        # If so, skip the network call — another request would just extend the ban.
        walmart_monitor = engine._monitors.get("walmart")
        if walmart_monitor and walmart_monitor.is_rate_limited():
            remaining = walmart_monitor.rate_limit_remaining()
            return {
                "ok": False,
                "message": (
                    f"Walmart is rate limiting us (429). "
                    f"All Walmart requests paused — wait {remaining:.0f}s before testing again."
                ),
            }

        session = db.get_retailer_session(user["id"], "walmart")
        if not session or not session.get("cookies_json"):
            return {
                "ok": False,
                "message": (
                    "Walmart blocks automated browsers with CAPTCHA. "
                    "You must import session cookies instead:\n"
                    "1. Log into walmart.com in your browser\n"
                    "2. Open DevTools (F12) > Application > Cookies\n"
                    "3. Copy all cookies using a browser extension (e.g. EditThisCookie)\n"
                    "4. Paste them in Settings > Session Cookies > Walmart > Import"
                ),
            }

        try:
            cookies = _json.loads(session["cookies_json"])
        except Exception:
            return {"ok": False, "message": "Saved session cookies are corrupted — re-import them"}

        if not cookies:
            return {"ok": False, "message": "No session cookies found — import them via Settings > Session Cookies"}

        # Single attempt — no retry loop.  If we get 429, record it on the
        # shared monitor so the entire system backs off together.
        try:
            async with httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(15.0),
                http2=True,
            ) as client:
                for name, value in cookies.items():
                    client.cookies.set(name, str(value), domain=".walmart.com")

                req_headers = {
                    **DEFAULT_HEADERS,
                    "Accept": "application/json",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "Referer": "https://www.walmart.com/",
                }

                resp = await client.get(
                    "https://www.walmart.com/orchestra/api/ccm/v3/bootstrap"
                    "?configNames=identity",
                    headers=req_headers,
                )

                if resp.status_code == 429:
                    # Parse optional Retry-After header
                    retry_after = None
                    retry_after_val = resp.headers.get("Retry-After")
                    if retry_after_val:
                        try:
                            retry_after = float(retry_after_val)
                        except (ValueError, TypeError):
                            pass
                    # Record on shared monitor so the monitor loop backs off too
                    if walmart_monitor:
                        walmart_monitor.record_rate_limit(retry_after)
                        remaining = walmart_monitor.rate_limit_remaining()
                    else:
                        remaining = 60
                    return {
                        "ok": False,
                        "message": (
                            f"Walmart rate limited (429). "
                            f"All Walmart requests paused for {remaining:.0f}s. "
                            f"Wait and try again."
                        ),
                    }

                if resp.status_code == 200:
                    data = resp.json()
                    # Check if identity config indicates logged in
                    identity = data.get("identity", {})
                    is_logged_in = identity.get("isLoggedIn", False)
                    if walmart_monitor:
                        walmart_monitor.record_success()
                    if is_logged_in:
                        logger.info("Walmart: session cookies valid (logged in)")
                        return {"ok": True, "message": f"Walmart session valid — {len(cookies)} cookies active"}

                    # Even if not definitively logged in, 200 means cookies aren't expired
                    logger.info("Walmart: session cookies accepted (HTTP 200)")
                    return {"ok": True, "message": f"Walmart session cookies accepted ({len(cookies)} cookies) — if checkout fails, re-import fresh cookies"}

                elif resp.status_code == 403:
                    return {
                        "ok": False,
                        "message": "Walmart session cookies expired or blocked (403). Re-import fresh cookies from your browser.",
                    }
                else:
                    return {
                        "ok": False,
                        "message": f"Walmart session check returned HTTP {resp.status_code}. Try re-importing cookies.",
                    }

        except Exception as exc:
            logger.error("Walmart session validation error: %s", exc)
            return {"ok": False, "message": f"Could not validate Walmart session: {exc}"}

    async def _test_costco_session(user: dict, email: str, password: str):
        """Test Costco account by validating imported session cookies via API.

        Costco uses Akamai Bot Manager which blocks headless browser logins.
        First try validating session cookies via the /gettoken endpoint.
        Fall back to browser-based login if no cookies are imported.
        """
        import json as _json
        import httpx
        from pmon.monitors.base import DEFAULT_HEADERS

        session = db.get_retailer_session(user["id"], "costco")
        if not session or not session.get("cookies_json"):
            # No session cookies — Akamai blocks headless browsers from even
            # loading costco.com, so browser login won't work.  Direct the
            # user to import session cookies instead.
            return {
                "ok": False,
                "message": (
                    "Costco blocks automated browsers (Akamai Bot Manager). "
                    "You must import session cookies instead:\n"
                    "1. Log into costco.com in your browser\n"
                    "2. Open DevTools (F12) > Application > Cookies\n"
                    "3. Copy all cookies using a browser extension (e.g. EditThisCookie)\n"
                    "4. Paste them in Settings > Session Cookies > Costco > Import"
                ),
            }

        try:
            cookies = _json.loads(session["cookies_json"])
        except Exception:
            return {"ok": False, "message": "Saved Costco session cookies are corrupted — re-import them"}

        if not cookies:
            return {"ok": False, "message": "No Costco session cookies found — import them via Settings > Session Cookies"}

        try:
            async with httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(15.0),
                http2=True,
            ) as client:
                for name, value in cookies.items():
                    client.cookies.set(name, str(value), domain=".costco.com")

                # Check session via /gettoken endpoint
                resp = await client.get(
                    "https://www.costco.com/gettoken",
                    headers={
                        **DEFAULT_HEADERS,
                        "Accept": "application/json",
                        "Referer": "https://www.costco.com/",
                    },
                )

                if resp.status_code == 403:
                    return {
                        "ok": False,
                        "message": "Costco session blocked by Akamai (403). Re-import fresh cookies from your browser.",
                    }

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if data.get("loggedIn") or data.get("userId") or data.get("token"):
                            return {"ok": True, "message": f"Costco session valid — {len(cookies)} cookies active"}
                    except Exception:
                        pass

                # Fallback: try /myaccount
                acct_resp = await client.get(
                    "https://www.costco.com/myaccount",
                    headers={
                        **DEFAULT_HEADERS,
                        "Accept": "text/html,*/*",
                        "Referer": "https://www.costco.com/",
                    },
                    follow_redirects=False,
                )

                if acct_resp.status_code == 302:
                    location = acct_resp.headers.get("location", "")
                    if "LogonForm" in location or "login" in location.lower():
                        return {
                            "ok": False,
                            "message": "Costco session expired — re-import cookies from your browser.",
                        }

                if acct_resp.status_code == 200:
                    return {"ok": True, "message": f"Costco session valid — {len(cookies)} cookies active"}

                return {
                    "ok": False,
                    "message": f"Costco session check returned HTTP {resp.status_code}. Try re-importing cookies.",
                }

        except Exception as exc:
            logger.error("Costco session validation error: %s", exc)
            return {"ok": False, "message": f"Could not validate Costco session: {exc}"}

    async def _bestbuy_test_verification(page, user: dict, email_sel: str, pass_sel: str,
                                          vision_fill, vision_click, vision_read_page):
        """Handle Best Buy's identity verification step during test login.

        After submitting email, Best Buy may ask for last 4 digits of phone
        and last name before showing the password field.
        """
        from pmon.checkout.human_behavior import (
            human_click_element, human_type, random_delay, wait_for_button_enabled, wait_for_page_ready,
        )

        # Check if verification fields are visible
        phone_selectors = (
            'input[id*="phone" i], input[name*="phone" i], '
            'input[id*="last4" i], input[name*="last4" i], '
            'input[id*="lastDigits" i], input[name*="lastDigits" i], '
            'input[id*="phoneLast" i], input[name*="phoneLast" i]'
        )
        last_name_selectors = (
            'input[id*="lastName" i], input[name*="lastName" i], '
            'input[id*="last_name" i], input[name*="last_name" i], '
            'input[id*="familyName" i], input[name*="familyName" i]'
        )

        # First check: is the password field already visible? If so, no verification needed.
        try:
            if await page.locator(pass_sel).first.is_visible(timeout=1500):
                return
        except Exception:
            pass

        phone_field_found = False
        try:
            phone_loc = page.locator(phone_selectors)
            phone_field_found = await phone_loc.first.is_visible(timeout=3000)
        except Exception:
            pass

        if not phone_field_found:
            # Check if auth picker or OTP page is visible instead — skip verification
            try:
                picker_visible = await page.locator(
                    'text=/choose.*sign.?in/i, text=/use password/i, '
                    'text=/one-time code/i, label:has-text("Use password")'
                ).first.is_visible(timeout=500)
                if picker_visible:
                    logger.info("Best Buy test: auth picker visible, skipping verification")
                    return
            except Exception:
                pass

            # Check via page text if there's a verification prompt
            try:
                body_text = await page.locator("body").first.inner_text(timeout=2000)
                body_lower = (body_text or "").lower()
                if "last 4 digits" not in body_lower and "verify your identity" not in body_lower:
                    return  # No verification step detected
                # Text suggests verification but selectors didn't find fields — try vision
                phone_field_found = True
            except Exception:
                return

        # Load verification data from the user's stored account
        accounts = db.get_retailer_accounts(user["id"])
        acc = accounts.get("bestbuy", {})
        phone_last4 = acc.get("phone_last4", "")
        account_last_name = acc.get("account_last_name", "")

        if not phone_last4 or not account_last_name:
            logger.warning("Best Buy test login: verification step detected but phone_last4 or account_last_name not configured")
            return

        logger.info("Best Buy test login: identity verification step detected — filling phone last 4 + last name")

        # Fill phone last 4
        filled_phone = False
        try:
            phone_loc = page.locator(phone_selectors).first
            if await phone_loc.is_visible(timeout=2000):
                await human_click_element(page, phone_loc)
                await random_delay(page, 100, 250)
                await human_type(page, phone_last4)
                filled_phone = True
        except Exception:
            pass
        if not filled_phone:
            await vision_fill(page, "last 4 digits of phone number input", phone_last4)

        await random_delay(page, 300, 600)

        # Fill last name
        filled_name = False
        try:
            name_loc = page.locator(last_name_selectors).first
            if await name_loc.is_visible(timeout=2000):
                await human_click_element(page, name_loc)
                await random_delay(page, 100, 250)
                await human_type(page, account_last_name)
                filled_name = True
        except Exception:
            pass
        if not filled_name:
            await vision_fill(page, "last name input", account_last_name)

        await random_delay(page, 300, 600)

        # Submit verification
        await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
        submit_clicked = False
        for btn_text in ["Continue", "Verify", "Submit", "Next"]:
            try:
                btn = page.get_by_role("button", name=btn_text, exact=False)
                if await btn.first.is_visible(timeout=500):
                    await btn.first.click()
                    submit_clicked = True
                    break
            except Exception:
                continue
        if not submit_clicked:
            try:
                submit_btn = page.locator('button[type="submit"]')
                if await submit_btn.first.is_visible(timeout=1000):
                    await submit_btn.first.click()
                    submit_clicked = True
            except Exception:
                pass
        if not submit_clicked:
            await vision_click(page, "Continue or Verify button")

        await wait_for_page_ready(page, timeout=10000)
        logger.info("Best Buy test login: verification step submitted")

    async def _poll_for_otp_code(otp_id: int, timeout_seconds: int = 300) -> str | None:
        """Poll the database for a submitted OTP code.

        Returns the code string if submitted, or None on timeout.
        """
        import asyncio
        import time
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            code = db.get_otp_code(otp_id)
            if code:
                return code
            await asyncio.sleep(3)
        return None

    async def _enter_otp_code(page, code: str):
        """Enter an OTP code into the current page's input field and submit."""
        from pmon.checkout.human_behavior import wait_for_page_ready
        try:
            otp_input = page.locator(
                'input[type="text"], input[type="tel"], input[type="number"], '
                'input[inputmode="numeric"], input[autocomplete="one-time-code"]'
            ).first
            await otp_input.click()
            await otp_input.fill(code)
            import asyncio
            await asyncio.sleep(0.5)
            # Try clicking submit/verify/continue
            for btn_text in ["Continue", "Verify", "Submit", "Sign In"]:
                try:
                    btn = page.get_by_role("button", name=btn_text, exact=False)
                    if await btn.first.is_visible(timeout=500):
                        await btn.first.click()
                        break
                except Exception:
                    continue
            else:
                # Fallback: click any submit button
                try:
                    await page.locator('button[type="submit"]').first.click()
                except Exception:
                    pass
            await wait_for_page_ready(page, timeout=10000)
        except Exception as e:
            logger.error("Failed to enter OTP code on page: %s", e)

    async def _test_login_browser(retailer: str, email: str, password: str, user: dict):
        """Test retailer login using Playwright browser automation."""
        import base64
        import json
        import os

        from pmon.checkout.human_behavior import (
            human_click_element,
            human_type,
            idle_scroll,
            random_delay,
            random_mouse_jitter,
            sweep_popups,
            wait_for_button_enabled,
            wait_for_page_ready,
            wait_for_url_change,
        )
        from pmon.checkout.network_monitor import NetworkMonitor

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
            # Target has NO dedicated /login page — it redirects to homepage.
            # Login is done via a side-panel opened from the account icon.
            # Set to None so we skip direct-URL navigation and go straight
            # to the homepage → click-sign-in-link approach.
            "target": None,
            "walmart": "https://identity.walmart.com/signin",
            "bestbuy": "https://www.bestbuy.com/identity/global/signin",
            # Pokemon Center: login is a modal on the homepage, NOT a standalone
            # page.  Navigating to /account/login triggers the WAF block page.
            # Use None to force the homepage → click-sign-in-link approach.
            "pokemoncenter": None,
            # Costco: direct navigation to /LogonForm or signin.costco.com
            # gets blocked. Must go to homepage and click the account link.
            "costco": None,
        }

        # Fallback: navigate to homepage and click sign-in link if direct URL fails
        HOME_URLS = {
            "target": "https://www.target.com",
            "walmart": "https://www.walmart.com",
            "bestbuy": "https://www.bestbuy.com",
            "pokemoncenter": "https://www.pokemoncenter.com",
            "costco": "https://www.costco.com",
        }

        SIGNIN_LINK_SELECTORS = {
            "target": '[data-test="@web/AccountLink"], #account, #accountNav, a[href*="/account"], a:has-text("Sign in"), button:has-text("Sign in"), a[href*="/login"], [data-test="accountNav-signIn"], [data-test="@web/AccountLink-signIn"]',
            "walmart": 'a[href*="/account/login"], a[href*="/account"], button:has-text("Sign In"), a:has-text("Sign In")',
            "bestbuy": 'a[href*="/signin"], a[href*="/identity"], a:has-text("Sign In"), .account-button',
            "pokemoncenter": 'a[href*="/account/login"], a[href*="/account"], a:has-text("Sign In"), a:has-text("Log In")',
            "costco": 'a[href*="/LogonForm"], a[href*="/login"], a:has-text("Sign In"), a:has-text("Sign In / Register")',
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
                "email": 'input[name="email"], input[type="email"], input[id*="email" i], input[type="tel"], input[name="phone"], input[id*="phone" i], #phone-number, input[autocomplete="tel"], input[autocomplete="username"]',
                "password": 'input[type="password"], input[name="password"], input[id*="password" i]',
                "submit": 'button[type="submit"], button:has-text("Sign in"), button:has-text("Continue"), button[data-automation-id="signin-submit-btn"]',
                "success": 'a[href*="/account"], [data-automation-id="account"], [data-tl-id*="account"], [data-testid*="account"]',
                "error": '[data-automation-id="error"], .error-message, [class*="error" i], [role="alert"], [data-testid*="error"]',
            },
            "bestbuy": {
                "email": '#fld-e, input[id="user.emailAddress"], input[type="email"], input[name="email"]',
                "password": '#fld-p1, input[type="password"], input[name="password"]',
                "submit": 'button[type="submit"], button:has-text("Sign In"), button:has-text("Verify"), button:has-text("Continue")',
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
            "costco": {
                "email": 'input[name="logonId"], #logonId, input[type="email"], input[name="email"], input[id*="email" i]',
                "password": 'input[name="logonPassword"], #logonPassword, input[type="password"], input[name="password"]',
                "submit": 'input[type="submit"], button[type="submit"], button:has-text("Sign In"), button:has-text("Sign In / Register")',
                "success": 'a[href*="/myaccount"], a[href*="/AccountStatusView"], [id*="myaccount" i], a:has-text("My Account"), a:has-text("My Orders")',
                "error": '.error-message, [class*="error" i], [role="alert"], .field-error',
            },
        }

        sel = SELECTORS[retailer]
        retailer_name = {"target": "Target", "walmart": "Walmart", "bestbuy": "Best Buy", "pokemoncenter": "Pokemon Center", "costco": "Costco"}[retailer]

        # Import shared stealth JS and Chrome version from checkout engine
        from pmon.checkout.engine import STEALTH_JS
        from pmon.monitors.base import _CHROME_FULL, _CHROME_MAJOR

        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-features=VizDisplayCompositor",
                    # Suppress automation-related flags that leak to JS/CDP
                    "--disable-infobars",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--no-first-run",
                    # GPU flags to avoid headless WebGL fingerprint leaks
                    "--use-gl=angle",
                    "--use-angle=d3d11",
                ],
            )

            # Create browser context WITHOUT storage_state first (avoids blank page
            # caused by malformed cookies).  We'll add cookies AFTER context creation
            # which is more resilient to bad data.
            context = await browser.new_context(
                user_agent=(
                    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{_CHROME_FULL} Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                screen={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="light",
                extra_http_headers={
                    "Sec-Ch-Ua": f'"Chromium";v="{_CHROME_MAJOR}", "Google Chrome";v="{_CHROME_MAJOR}", "Not?A_Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                },
            )
            await context.add_init_script(STEALTH_JS)

            # Load previously saved session cookies AFTER context creation.
            # Using add_cookies() instead of storage_state avoids blank page issues
            # when cookie data is malformed or has unexpected fields.
            try:
                existing_session = db.get_retailer_session(user["id"], retailer)
                if existing_session and existing_session.get("cookies_json"):
                    import json as _json
                    saved_cookies = _json.loads(existing_session["cookies_json"])
                    if saved_cookies:
                        domain_map = {
                            "target": ".target.com",
                            "walmart": ".walmart.com",
                            "bestbuy": ".bestbuy.com",
                            "pokemoncenter": ".pokemoncenter.com",
                            "costco": ".costco.com",
                        }
                        pw_cookies = []
                        for name, value in saved_cookies.items():
                            pw_cookies.append({
                                "name": str(name),
                                "value": str(value),
                                "domain": domain_map.get(retailer, f".{retailer}.com"),
                                "path": "/",
                            })
                        await context.add_cookies(pw_cookies)
                        logger.info("Test login %s: loaded %d saved session cookies", retailer_name, len(pw_cookies))
            except Exception as exc:
                logger.debug("Could not load saved session for test login: %s (continuing without)", exc)

            page = await context.new_page()

            try:
                # --- Warm up: visit homepage first to establish cookies ---
                # PerimeterX flags sessions that jump straight to /login without
                # ever loading the homepage.  A quick homepage visit first reduces
                # the chance of being blocked on the login page.
                try:
                    await page.goto(HOME_URLS[retailer], wait_until="domcontentloaded", timeout=30000)
                    await wait_for_page_ready(page, timeout=15000)
                    # Human-like: move mouse around, scroll, dwell on homepage
                    await random_mouse_jitter(page)
                    await idle_scroll(page)
                    await random_delay(page, 1000, 3000)
                    logger.info("Test login %s: homepage warm-up OK", retailer_name)
                except Exception as warm_err:
                    logger.debug("Test login %s: homepage warm-up failed: %s", retailer_name, warm_err)

                # --- Navigate to login page ---
                landed_on_login = False
                nav_failed = False

                login_url = LOGIN_URLS.get(retailer)

                if login_url is not None:
                    # Retailer has a dedicated login URL — try it
                    try:
                        await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        nav_failed = True

                    if not nav_failed:
                        await wait_for_page_ready(page, timeout=15000)

                        # Check for blank page — can happen when cookies/session cause
                        # a redirect loop or when PerimeterX blocks with an empty response
                        try:
                            page_content = await page.content()
                            if not page_content or len(page_content.strip()) < 100 or page_content.strip() == "<html><head></head><body></body></html>":
                                logger.warning("Test login %s: blank page detected after navigation — retrying without cookies", retailer_name)
                                await context.clear_cookies()
                                await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
                                await wait_for_page_ready(page, timeout=10000)
                        except Exception:
                            pass
                else:
                    # No dedicated login URL (e.g. Target) — go straight to
                    # homepage sign-in panel approach
                    nav_failed = True
                    logger.info("Test login %s: no dedicated login URL — will use homepage sign-in panel", retailer_name)

                current_url = page.url

                # Determine if we actually landed on a login page
                login_indicators = ["/login", "/signin", "/sign-in", "/identity", "access.pokemon.com", "sso.pokemon.com", "identity.walmart.com", "/logonform", "signin.costco.com"]
                landed_on_login = any(ind in current_url.lower() for ind in login_indicators)

                # Walmart: verifyToken means OAuth completed — NOT a login page
                if retailer == "walmart" and ("verifytoken" in current_url.lower() or "action=signin" in current_url.lower()):
                    landed_on_login = False

                # Check for bot-block pages (URL or page content)
                if "blocked" in current_url or "captcha" in current_url or "challenge" in current_url:
                    landed_on_login = False

                # Content-level bot block detection (applies to any page, not just login)
                async def _check_bot_block(pg, context_msg: str = "") -> dict | None:
                    """Check current page for bot-block / error indicators.
                    Returns an error response dict if blocked, None if OK."""
                    try:
                        body_text = await pg.locator("body").first.inner_text(timeout=2000)
                        body_lower = body_text.lower() if body_text else ""
                        block_phrases = [
                            "unusual activity",
                            "access to this page has been denied",
                            "temporarily restricted",
                            "bot activity",
                            "your ip",
                            "technical issues",
                            "technical difficulties",
                            "we're having technical",
                            "something went wrong",
                            "access denied",
                            "please verify you are a human",
                            "are you a robot",
                        ]
                        if any(phrase in body_lower for phrase in block_phrases):
                            page_desc = await vision_read_page(pg)
                            msg = f"{retailer_name}: access blocked (bot/IP detection)"
                            if context_msg:
                                msg = f"{retailer_name}: {context_msg}"
                            if page_desc:
                                msg += f" — {page_desc}"
                            msg += (
                                ". Tip: import session cookies from your browser instead "
                                "(Settings > Session Cookies)."
                            )
                            return {"ok": False, "message": msg}
                    except Exception:
                        pass
                    return None

                if landed_on_login:
                    block_result = await _check_bot_block(page)
                    if block_result:
                        return block_result

                # --- Fallback: if redirected away from login, go to homepage and find sign-in link/panel ---
                if not landed_on_login or nav_failed:
                    # Before retrying, check if we're on a bot-block page
                    # (skip this check if we intentionally skipped direct URL — login_url is None)
                    if login_url is not None:
                        block_result = await _check_bot_block(page, "blocked before reaching login page")
                        if block_result:
                            return block_result

                    logger.info("Test login %s: using homepage sign-in panel approach (current: %s)", retailer_name, current_url)

                    # Only re-navigate if we're not already on the homepage
                    # (we already visited it during warm-up)
                    home_url = HOME_URLS[retailer]
                    if not current_url.rstrip("/").endswith(home_url.rstrip("/").split("//", 1)[-1].rstrip("/")):
                        try:
                            await page.goto(home_url, wait_until="domcontentloaded", timeout=45000)
                        except Exception as home_err:
                            page_desc = await vision_read_page(page)
                            msg = f"{retailer_name} page failed to load"
                            if page_desc:
                                msg += f": {page_desc}"
                            return {"ok": False, "message": msg}

                    await wait_for_page_ready(page, timeout=15000)

                    # Human-like: browse the page before clicking sign-in
                    await random_mouse_jitter(page)
                    await random_delay(page, 500, 1500)

                    # Check homepage for bot block too
                    block_result = await _check_bot_block(page, "homepage blocked by bot detection")
                    if block_result:
                        return block_result

                    # Try to click the sign-in link/icon from the homepage
                    # For Target this opens a slide-in side panel
                    signin_clicked = False
                    try:
                        signin_link = page.locator(SIGNIN_LINK_SELECTORS[retailer])
                        if await signin_link.first.is_visible(timeout=8000):
                            await human_click_element(page, signin_link)
                            signin_clicked = True
                            # Wait for the side panel / login form to appear
                            await wait_for_page_ready(page, timeout=10000)
                            await random_delay(page, 1000, 2000)
                    except Exception:
                        pass

                    if not signin_clicked:
                        # Vision fallback: find and click sign-in link on homepage
                        if await vision_click(page, "Sign in or Account link/icon in the top navigation"):
                            signin_clicked = True
                            await wait_for_page_ready(page, timeout=10000)
                            await random_delay(page, 1000, 2000)

                    # For Target: after clicking account icon, a side panel appears
                    # with "Sign in or create account" button — click it
                    if signin_clicked and retailer == "target":
                        try:
                            sign_in_btn = page.locator(
                                'a:has-text("Sign in or create account"), '
                                'button:has-text("Sign in or create account"), '
                                'a[href*="/login"], '
                                '[data-test="accountNav-signIn"]'
                            )
                            if await sign_in_btn.first.is_visible(timeout=5000):
                                await human_click_element(page, sign_in_btn)
                                logger.info("Test login %s: clicked 'Sign in or create account' in side panel", retailer_name)
                                await wait_for_page_ready(page, timeout=15000)
                                await random_delay(page, 500, 1500)
                        except Exception:
                            # Vision fallback for the sign-in button in the panel
                            await vision_click(page, "Sign in or create account button")
                            await wait_for_page_ready(page, timeout=15000)

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

                # --- Dismiss overlay modals that block interaction ---
                # Uses shared sweep_popups() which handles cookie consent,
                # sign-in prompts, store pickers, age gates, health consent,
                # and generic dialogs with JS fallback.
                dismissed = await sweep_popups(page)
                if dismissed:
                    logger.info("Test login %s: dismissed %d popup(s)", retailer_name, dismissed)

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
                        await random_delay(page, 800, 1200)
                    else:
                        page_desc = await vision_read_page(page)
                        msg = f"{retailer_name} login page did not load — no email/username field found at {current_url}"
                        if page_desc:
                            msg += f" (page shows: {page_desc})"
                        return {"ok": False, "message": msg}

                # Enter email — human-like typing with verification
                if email_found and not await _page_has_value(page, email_sel, email):
                    email_input = page.locator(email_sel).first
                    # Move mouse to field and click like a human
                    await human_click_element(page, email_input)
                    await random_delay(page, 150, 350)
                    await email_input.press("Control+a")
                    # Type with variable speed (not fixed delay)
                    await human_type(page, email)
                    await random_delay(page, 300, 600)

                    # Verify it actually took — Target's JS can clear or reset the field
                    if not await _page_has_value(page, email_sel, email):
                        logger.warning("Test login %s: human_type() did not fill email — using fill()", retailer_name)
                        await email_input.fill(email)
                        await random_delay(page, 200, 400)
                        # Final check — if still empty, try triple-click + type
                        if not await _page_has_value(page, email_sel, email):
                            await email_input.click(click_count=3, force=True)
                            await human_type(page, email)
                            await random_delay(page, 200, 400)

                # Check if password field is visible yet (single-step) or needs submit first (multi-step)
                pass_visible = False
                try:
                    pass_visible = await page.locator(pass_sel).first.is_visible(timeout=1500)
                except Exception:
                    pass

                if pass_visible:
                    # Single-step: fill both and submit — human-like
                    pw_loc = page.locator(pass_sel).first
                    await human_click_element(page, pw_loc)
                    await random_delay(page, 150, 300)
                    await human_type(page, password)
                    await random_delay(page, 200, 500)
                    # Verify password was entered; if empty, fall back to fill()
                    try:
                        pw_val = await pw_loc.input_value(timeout=1000)
                        if not pw_val:
                            await pw_loc.fill(password)
                            await random_delay(page, 200, 400)
                    except Exception:
                        pass
                    # Wait for submit button to be enabled (grayed-out fix)
                    await wait_for_button_enabled(page, 'button[type="submit"]', timeout=15000)
                    await random_delay(page, 100, 300)
                    # Try selector click, then get_by_role, then vision
                    single_submit_clicked = await click_visible_button(page, submit_sel)
                    if not single_submit_clicked:
                        for btn_text in ["Sign In", "Sign in", "Verify", "Continue", "Log In", "Submit"]:
                            try:
                                btn = page.get_by_role("button", name=btn_text, exact=False)
                                if await btn.first.is_visible(timeout=500):
                                    await btn.first.click()
                                    single_submit_clicked = True
                                    break
                            except Exception:
                                continue
                    if not single_submit_clicked:
                        await vision_click(page, "Sign In / Continue / Verify button")
                else:
                    # Check if auth method picker is already visible (e.g. Walmart with pre-filled phone)
                    # If so, skip email submit and go straight to auth method selection
                    auth_picker_already_visible = False
                    try:
                        picker_text = page.get_by_text("Choose a sign in method", exact=False)
                        if await picker_text.first.is_visible(timeout=1000):
                            auth_picker_already_visible = True
                            logger.info("Test login %s: auth method picker already visible (skipping email submit)", retailer_name)
                    except Exception:
                        pass
                    if not auth_picker_already_visible:
                        try:
                            pw_radio = page.get_by_role("radio", name="Password", exact=False)
                            if await pw_radio.first.is_visible(timeout=500):
                                auth_picker_already_visible = True
                                logger.info("Test login %s: password radio already visible (skipping email submit)", retailer_name)
                        except Exception:
                            pass

                    # Multi-step: submit email/phone first (unless auth picker already showing)
                    if not auth_picker_already_visible:
                        # Wait for submit button to be enabled (grayed-out fix)
                        await wait_for_button_enabled(page, 'button[type="submit"]', timeout=15000)
                        await random_delay(page, 100, 300)
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

                        # Wait for page to respond (not a fixed 3s wait)
                        await wait_for_page_ready(page, timeout=10000)

                        # Sweep popups that may have appeared after email submit
                        await sweep_popups(page)

                        # Log what page we're on after submit attempt
                        post_submit_url = page.url
                        logger.info("Test login %s: after submit, URL is %s", retailer_name, post_submit_url)

                        # Check if we navigated away from login entirely (e.g. Target redirects to homepage)
                        # This can mean: already logged in, or email accepted and session created
                        login_path_indicators = ["/login", "/signin", "/sign-in", "/identity", "access.pokemon.com", "sso.pokemon.com", "identity.walmart.com"]
                        still_on_login_after_email = any(ind in post_submit_url.lower() for ind in login_path_indicators)

                        # Walmart: verifyToken/action=SignIn means OAuth succeeded
                        if retailer == "walmart" and still_on_login_after_email:
                            if "verifytoken" in post_submit_url.lower() or "action=signin" in post_submit_url.lower():
                                still_on_login_after_email = False

                        if not still_on_login_after_email:
                            logger.info("Test login %s: navigated away from login after email submit (URL: %s) — checking if logged in", retailer_name, post_submit_url)
                            # Check for success indicators on the page we landed on
                            success_el = page.locator(sel["success"])
                            try:
                                if await success_el.first.is_visible(timeout=3000):
                                    logger.info("Test login %s: success indicator found after email redirect — already logged in", retailer_name)
                                    # Save cookies and return success
                                    try:
                                        browser_cookies = await context.cookies()
                                        cookies_dict = {c["name"]: c["value"] for c in browser_cookies if c.get("name") and c.get("value")}
                                        if cookies_dict:
                                            import json as _json
                                            db.set_retailer_session(user["id"], retailer, cookies_json=_json.dumps(cookies_dict))
                                            if engine.checkout_engine:
                                                engine.checkout_engine._api.load_session_cookies(retailer, cookies_dict)
                                                engine.checkout_engine._api.reset_client(retailer)
                                            logger.info("Auto-saved %d session cookies for %s (user %s)", len(cookies_dict), retailer, user["username"])
                                    except Exception as cookie_err:
                                        logger.warning("Failed to auto-save cookies for %s: %s", retailer_name, cookie_err)
                                    return {"ok": True, "message": f"{retailer_name} login successful — already signed in, session cookies saved"}
                            except Exception:
                                pass

                            # No success indicator but we're on the homepage — use vision to check
                            page_desc = await vision_read_page(page)
                            # If page looks like a normal homepage (not an error), treat as likely success
                            # since Target redirects to homepage after successful auth
                            if page_desc:
                                desc_lower = page_desc.lower()
                                error_phrases = ["error", "incorrect", "invalid", "denied", "blocked", "failed"]
                                if not any(phrase in desc_lower for phrase in error_phrases):
                                    logger.info("Test login %s: on homepage with no errors — treating as success (page: %s)", retailer_name, page_desc[:100])
                                    try:
                                        browser_cookies = await context.cookies()
                                        cookies_dict = {c["name"]: c["value"] for c in browser_cookies if c.get("name") and c.get("value")}
                                        if cookies_dict:
                                            import json as _json
                                            db.set_retailer_session(user["id"], retailer, cookies_json=_json.dumps(cookies_dict))
                                            if engine.checkout_engine:
                                                engine.checkout_engine._api.load_session_cookies(retailer, cookies_dict)
                                                engine.checkout_engine._api.reset_client(retailer)
                                            logger.info("Auto-saved %d session cookies for %s (user %s)", len(cookies_dict), retailer, user["username"])
                                    except Exception as cookie_err:
                                        logger.warning("Failed to auto-save cookies for %s: %s", retailer_name, cookie_err)
                                    return {"ok": True, "message": f"{retailer_name} login successful — redirected to homepage, session cookies saved"}
                                else:
                                    msg = f"{retailer_name} login may have failed — redirected to: {page_desc[:200]}"
                                    return {"ok": False, "message": msg}

                        # Check for "Something went wrong" error and retry once
                        error_banner = page.locator('[role="alert"], .error-message, [class*="error" i], [data-test="error"]')
                        try:
                            if await error_banner.first.is_visible(timeout=2000):
                                banner_text = await error_banner.first.inner_text(timeout=1000)
                                if "something went wrong" in banner_text.lower() or "try again" in banner_text.lower():
                                    logger.info("Test login %s: server error on first attempt, retrying", retailer_name)
                                    await random_delay(page, 1500, 2500)
                                    # Clear and re-type email, then submit again
                                    try:
                                        email_loc = page.locator(email_sel).first
                                        await human_click_element(page, email_loc)
                                        await email_loc.press("Control+a")
                                        await human_type(page, email)
                                        await random_delay(page, 300, 600)
                                        # Re-use the same multi-strategy click
                                        await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
                                        for btn_text in ["Continue with email", "Continue", "Sign in"]:
                                            try:
                                                btn = page.get_by_role("button", name=btn_text, exact=False)
                                                if await btn.first.is_visible(timeout=500):
                                                    await human_click_element(page, btn)
                                                    break
                                            except Exception:
                                                continue
                                        await wait_for_page_ready(page, timeout=10000)
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                    # --- Best Buy identity verification step ---
                    # After submitting email, Best Buy may ask for last 4 digits of phone
                    # and last name before showing the password field.
                    if retailer == "bestbuy":
                        await _bestbuy_test_verification(page, user, email_sel, pass_sel, vision_fill, vision_click, vision_read_page)

                    # Some sites show an auth method picker before the password field:
                    # - Target: links/buttons "Enter your password", "Use a passkey", "Get a code"
                    # - Walmart: radio buttons "Text me a verification code", "Email me a verification code", "Password"
                    # - Best Buy: tabs/options for text code, email code, Google, password
                    pw_option_clicked = False

                    # Re-dismiss overlays (Target's consent modal can reappear after email submit)
                    await sweep_popups(page)

                    # Check if password field is already visible (no picker needed)
                    try:
                        if await page.locator(pass_sel).first.is_visible(timeout=2000):
                            pw_option_clicked = True  # Skip picker, password field already showing
                            logger.info("Test login %s: password field already visible (no auth picker)", retailer_name)
                    except Exception:
                        pass

                    # Password auth option text variants
                    pw_texts = [
                        "Enter your password", "Enter password",
                        "Password", "Use password", "Use a password",
                        "Use your password", "Sign in with password",
                        "password",  # lowercase fallback
                    ]

                    # Strategy 0 (Best Buy specific): JS click for styled radio buttons
                    # Best Buy uses hidden radio inputs + visible labels — standard Playwright
                    # locators often miss them. Also handles divs, tabs, and other custom elements.
                    if not pw_option_clicked and retailer == "bestbuy":
                        try:
                            clicked_js = await page.evaluate("""() => {
                                // Labels with password text (Best Buy's primary pattern)
                                const labels = document.querySelectorAll('label');
                                for (const label of labels) {
                                    const text = (label.textContent || '').trim().toLowerCase();
                                    if (text === 'use password' || text === 'password'
                                        || text.includes('sign in with password')
                                        || text.includes('use a password')
                                        || text.includes('use your password')) {
                                        label.click();
                                        return 'LABEL: ' + label.textContent.trim().substring(0, 60);
                                    }
                                }
                                // Radio inputs with password value/id
                                const radios = document.querySelectorAll('input[type="radio"]');
                                for (const radio of radios) {
                                    const val = (radio.value || '').toLowerCase();
                                    const id = radio.id || '';
                                    if (val.includes('password') || id.toLowerCase().includes('password')) {
                                        const label = id ? document.querySelector('label[for="' + id + '"]') : null;
                                        if (label) { label.click(); return 'LABEL[for]: ' + label.textContent.trim().substring(0, 40); }
                                        radio.click();
                                        radio.dispatchEvent(new Event('change', {bubbles: true}));
                                        return 'RADIO: value=' + val;
                                    }
                                }
                                // Any clickable element with password text (not "forgot")
                                const allEls = document.querySelectorAll('label, span, div, a, button, li, p, [role="radio"], [role="option"], [role="tab"], [tabindex]');
                                const phrases = ['use password', 'use a password', 'use your password',
                                    'sign in with password', 'password'];
                                for (const el of allEls) {
                                    const text = (el.textContent || '').trim().toLowerCase();
                                    if (el.offsetParent === null) continue;
                                    if (text.includes('forgot')) continue;
                                    for (const phrase of phrases) {
                                        if (text === phrase || text.startsWith(phrase)) {
                                            el.click();
                                            return el.tagName + ': ' + (el.textContent || '').trim().substring(0, 60);
                                        }
                                    }
                                }
                                // Data attributes with password
                                const dataEls = document.querySelectorAll('[data-track*="password" i], [data-value*="password" i], [data-method*="password" i], [value*="password" i]');
                                for (const el of dataEls) {
                                    if (el.offsetParent !== null || el.type === 'radio') {
                                        const label = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
                                        if (label) { label.click(); return 'DATA-LABEL: ' + label.textContent.trim().substring(0, 40); }
                                        el.click();
                                        return 'DATA-ATTR: ' + el.tagName;
                                    }
                                }
                                return null;
                            }""")
                            if clicked_js:
                                pw_option_clicked = True
                                logger.info("Test login %s: clicked password option via BB JS: %s", retailer_name, clicked_js)
                        except Exception as e:
                            logger.debug("Test login %s: BB JS click failed: %s", retailer_name, e)

                    # Strategy 1: get_by_role("button") for button-style pickers
                    if not pw_option_clicked:
                        for option_text in pw_texts:
                            try:
                                opt = page.get_by_role("button", name=option_text, exact=False)
                                if await opt.first.is_visible(timeout=500):
                                    await opt.first.click(force=True)
                                    pw_option_clicked = True
                                    logger.info("Test login %s: clicked auth method via get_by_role('button', '%s')", retailer_name, option_text)
                                    break
                            except Exception:
                                continue

                    # Strategy 2: get_by_role("link") — Target often uses <a> tags for auth options
                    if not pw_option_clicked:
                        for option_text in pw_texts:
                            try:
                                opt = page.get_by_role("link", name=option_text, exact=False)
                                if await opt.first.is_visible(timeout=500):
                                    await opt.first.click(force=True)
                                    pw_option_clicked = True
                                    logger.info("Test login %s: clicked auth method via get_by_role('link', '%s')", retailer_name, option_text)
                                    break
                            except Exception:
                                continue

                    # Strategy 3: get_by_role("radio") for radio-button pickers (Walmart)
                    if not pw_option_clicked:
                        for option_text in ["Password"]:
                            try:
                                opt = page.get_by_role("radio", name=option_text, exact=False)
                                if await opt.first.is_visible(timeout=500):
                                    await opt.first.click(force=True)
                                    pw_option_clicked = True
                                    logger.info("Test login %s: clicked auth method via get_by_role('radio', '%s')", retailer_name, option_text)
                                    break
                            except Exception:
                                continue

                    # Strategy 4: get_by_text with exact=False (catches divs/spans/labels)
                    if not pw_option_clicked:
                        for option_text in pw_texts:
                            try:
                                opt = page.get_by_text(option_text, exact=False)
                                if await opt.first.is_visible(timeout=500):
                                    await opt.first.click(force=True)
                                    pw_option_clicked = True
                                    logger.info("Test login %s: clicked auth method via get_by_text('%s')", retailer_name, option_text)
                                    break
                            except Exception:
                                continue

                    # Strategy 5: CSS selectors (labels for radio, buttons, links, list items)
                    if not pw_option_clicked:
                        password_option = page.locator(
                            'button:has-text("password"), a:has-text("password"), '
                            '[data-test*="password" i], div:has-text("Enter your password"), '
                            'label:has-text("Password"), input[type="radio"][value*="password" i], '
                            'li:has-text("password"), span:has-text("Enter your password")'
                        )
                        try:
                            if await password_option.first.is_visible(timeout=1000):
                                await password_option.first.click(force=True)
                                pw_option_clicked = True
                                logger.info("Test login %s: clicked auth method via CSS selector", retailer_name)
                        except Exception:
                            pass

                    # Strategy 6: JS click — find any clickable element containing "password" text
                    if not pw_option_clicked:
                        try:
                            clicked_js = await page.evaluate("""() => {
                                const els = document.querySelectorAll('a, button, [role="button"], [role="link"], li, div[tabindex], span[tabindex]');
                                for (const el of els) {
                                    const text = (el.textContent || '').toLowerCase().trim();
                                    if (text.includes('password') && !text.includes('forgot') && el.offsetParent !== null) {
                                        el.click();
                                        return el.tagName + ': ' + text.substring(0, 60);
                                    }
                                }
                                return null;
                            }""")
                            if clicked_js:
                                pw_option_clicked = True
                                logger.info("Test login %s: clicked auth method via JS: %s", retailer_name, clicked_js)
                        except Exception:
                            pass

                    # Strategy 7: Vision fallback
                    if not pw_option_clicked:
                        logger.info("Test login %s: trying vision for auth method picker", retailer_name)
                        pw_option_clicked = await vision_click(page, "Password option (radio button or link to select password sign-in method)")

                    if pw_option_clicked:
                        await wait_for_page_ready(page, timeout=8000)
                    else:
                        # Dump page diagnostics so we can see what Best Buy is actually showing
                        logger.warning("Test login %s: could not find password auth method option", retailer_name)
                        try:
                            diag = await page.evaluate("""() => {
                                const info = {url: location.href, title: document.title};
                                // Collect all interactive elements with their text
                                const els = document.querySelectorAll('label, a, button, [role="radio"], [role="tab"], [role="option"], [role="button"], input[type="radio"], [tabindex]');
                                info.interactive = [];
                                for (const el of els) {
                                    const text = (el.textContent || '').trim().substring(0, 80);
                                    if (!text) continue;
                                    info.interactive.push({
                                        tag: el.tagName,
                                        role: el.getAttribute('role'),
                                        type: el.type || null,
                                        id: el.id || null,
                                        text: text,
                                        visible: el.offsetParent !== null,
                                    });
                                }
                                // Collect headings
                                const headings = document.querySelectorAll('h1, h2, h3');
                                info.headings = Array.from(headings).map(h => h.textContent.trim().substring(0, 100));
                                return info;
                            }""")
                            logger.info("Test login %s: auth picker page diagnostics: %s", retailer_name, diag)
                        except Exception as diag_err:
                            logger.debug("Test login %s: diagnostics failed: %s", retailer_name, diag_err)

                    # --- Pre-password OTP detection (Best Buy) ---
                    # If we're on the OTP page and none of the strategies clicked "Use password",
                    # try one more time with vision specifically for the OTP → password switch
                    if retailer == "bestbuy" and not pw_option_clicked:
                        otp_page = False
                        try:
                            otp_page = await page.locator(
                                'text=/one-time code/i, text=/enter your code/i, '
                                'text=/enter the code/i, text=/verification code/i'
                            ).first.is_visible(timeout=1500)
                        except Exception:
                            pass
                        if otp_page:
                            logger.warning("Test login %s: on OTP page — trying vision to find password option", retailer_name)
                            pw_option_clicked = await vision_click(
                                page,
                                "The 'Use password' or 'Password' option/radio/tab to switch from one-time code to password sign-in. Do NOT click any input fields — click the option to SELECT password as the method."
                            )
                            if pw_option_clicked:
                                await wait_for_page_ready(page, timeout=5000)
                            else:
                                # Can't switch to password — wait for OTP code from user
                                otp_id = db.create_otp_request(user["id"], retailer, context="test_login")
                                logger.info("Test login %s: OTP required (pre-login), waiting for code (otp_id=%d)", retailer_name, otp_id)
                                code = await _poll_for_otp_code(otp_id, timeout_seconds=300)
                                if code:
                                    await _enter_otp_code(page, code)
                                    logger.info("Test login %s: pre-login OTP code entered", retailer_name)
                                    # After OTP, we should be logged in — skip password entry
                                    pw_option_clicked = True
                                else:
                                    db.expire_otp_request(otp_id)
                                    return {"ok": False, "message": f"{retailer_name} verification code was not entered within 5 minutes."}

                    # Wait for password field — selectors first, then vision
                    pass_found = False
                    try:
                        await page.locator(pass_sel).first.wait_for(state="visible", timeout=10000)
                        pass_found = True
                    except Exception:
                        # Password field didn't appear — verification may show after auth picker
                        if retailer == "bestbuy":
                            await _bestbuy_test_verification(page, user, email_sel, pass_sel, vision_fill, vision_click, vision_read_page)
                            try:
                                await page.locator(pass_sel).first.wait_for(state="visible", timeout=5000)
                                pass_found = True
                            except Exception:
                                pass

                    if pass_found:
                        # Click the password field to focus it, then type — human-like
                        pw_locator = page.locator(pass_sel).first
                        await human_click_element(page, pw_locator)
                        await random_delay(page, 150, 300)
                        await human_type(page, password)
                        await random_delay(page, 200, 500)
                        # Verify password was entered; if empty, fall back to fill()
                        try:
                            pw_value = await pw_locator.input_value(timeout=1000)
                            if not pw_value:
                                logger.info("Test login %s: human_type() did not fill password, falling back to fill()", retailer_name)
                                await pw_locator.fill(password)
                                await random_delay(page, 200, 400)
                        except Exception:
                            pass
                        # Wait for submit button to be enabled (grayed-out fix)
                        await wait_for_button_enabled(page, 'button[type="submit"]', timeout=15000)
                        await random_delay(page, 100, 300)
                        # Multi-strategy submit after password entry
                        pw_submit_clicked = await click_visible_button(page, submit_sel)
                        if not pw_submit_clicked:
                            for btn_text in ["Sign In", "Sign in", "Verify", "Continue", "Log In", "Submit"]:
                                try:
                                    btn = page.get_by_role("button", name=btn_text, exact=False)
                                    if await btn.first.is_visible(timeout=500):
                                        await btn.first.click()
                                        pw_submit_clicked = True
                                        logger.info("Test login %s: clicked post-password submit via get_by_role('%s')", retailer_name, btn_text)
                                        break
                                except Exception:
                                    continue
                        if not pw_submit_clicked:
                            logger.info("Test login %s: trying vision for post-password submit", retailer_name)
                            await vision_click(page, "Sign In / Continue / Verify button")
                    else:
                        # Vision fallback for password entry
                        if await vision_fill(page, "password input", password):
                            await random_delay(page, 400, 700)
                            await vision_click(page, "Sign In / Continue / Verify button")
                        else:
                            page_desc = await vision_read_page(page)
                            msg = f"{retailer_name} login: submitted email but password field did not appear"
                            if page_desc:
                                msg += f" (page shows: {page_desc})"
                            return {"ok": False, "message": msg}

                # Wait for login to complete via network monitoring instead of fixed 5s
                # Monitors both Target OAuth (token_validations) and Walmart OAuth (verifyToken)
                net_monitor = NetworkMonitor(page)
                await net_monitor.start()
                pre_submit_url = page.url

                # Give the OAuth flow time to complete
                login_done = await net_monitor.wait_for_login_complete(timeout=15000, retailer=retailer)
                if not login_done:
                    # Fallback: wait for URL change
                    await wait_for_url_change(page, pre_submit_url, timeout=10000)

                # Check if PerimeterX blocked during login
                if net_monitor.was_blocked():
                    logger.warning("Test login %s: PerimeterX blocked %d request(s)", retailer_name, len(net_monitor.get_blocked_details()))

                await net_monitor.stop()

                # --- Post-login OTP detection (Best Buy) ---
                # Best Buy may require a one-time code AFTER password submission (2FA).
                # Detect this and return a clear error instead of misreporting success/failure.
                if retailer == "bestbuy":
                    # Give page time to settle after login network activity
                    await wait_for_page_ready(page, timeout=5000)
                    post_login_otp = False
                    try:
                        post_login_otp = await page.locator(
                            'text=/one-time code/i, text=/enter your code/i, '
                            'text=/enter the code/i, text=/verification code/i, '
                            'text=/enter your one-time/i'
                        ).first.is_visible(timeout=5000)
                    except Exception:
                        pass
                    if post_login_otp:
                        logger.warning("Test login %s: post-login OTP code requested after password accepted", retailer_name)
                        otp_id = db.create_otp_request(user["id"], retailer, context="test_login")
                        logger.info("Test login %s: OTP required (post-login), waiting for code (otp_id=%d)", retailer_name, otp_id)
                        code = await _poll_for_otp_code(otp_id, timeout_seconds=300)
                        if code:
                            await _enter_otp_code(page, code)
                            logger.info("Test login %s: post-login OTP code entered", retailer_name)
                            await wait_for_page_ready(page, timeout=10000)
                        else:
                            db.expire_otp_request(otp_id)
                            return {"ok": False, "message": f"{retailer_name} verification code was not entered within 5 minutes."}

                # Sweep any post-login popups
                await sweep_popups(page)

                # Check for success indicators
                final_url = page.url
                logger.info("Test login %s: final URL after submit: %s", retailer_name, final_url)

                still_on_login = "/login" in final_url or "/signin" in final_url or "/sign-in" in final_url or "/identity" in final_url

                # Walmart-specific: /account/verifyToken redirect or ?action=SignIn means
                # OAuth code exchange succeeded — treat as login success
                if retailer == "walmart" and still_on_login:
                    if "verifyToken" in final_url or "action=SignIn" in final_url:
                        still_on_login = False
                        logger.info("Test login %s: Walmart OAuth verifyToken/SignIn detected in URL", retailer_name)

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

                    # --- Auto-save session cookies for API checkout ---
                    try:
                        browser_cookies = await context.cookies()
                        cookies_dict = {c["name"]: c["value"] for c in browser_cookies if c.get("name") and c.get("value")}
                        if cookies_dict:
                            import json as _json
                            db.set_retailer_session(
                                user["id"], retailer,
                                cookies_json=_json.dumps(cookies_dict),
                            )
                            # Hot-reload into checkout engine if running
                            if engine.checkout_engine:
                                engine.checkout_engine._api.load_session_cookies(retailer, cookies_dict)
                                engine.checkout_engine._api.reset_client(retailer)
                            logger.info("Auto-saved %d session cookies for %s (user %s)", len(cookies_dict), retailer, user["username"])
                    except Exception as cookie_err:
                        logger.warning("Failed to auto-save cookies for %s: %s", retailer_name, cookie_err)

                    return {"ok": True, "message": f"{retailer_name} login successful — session cookies saved for API checkout"}

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
                    # Check if the "error" is actually an OTP prompt that we missed earlier
                    otp_keywords = ["one-time code", "enter your code", "enter the code",
                                    "verification code", "verify code", "verifycode"]
                    if retailer == "bestbuy" and any(kw in error_text.lower() for kw in otp_keywords):
                        logger.warning("Test login %s: OTP page detected via error text fallback", retailer_name)
                        otp_id = db.create_otp_request(user["id"], retailer, context="test_login")
                        logger.info("Test login %s: OTP required (fallback), waiting for code (otp_id=%d)", retailer_name, otp_id)
                        code = await _poll_for_otp_code(otp_id, timeout_seconds=300)
                        if code:
                            await _enter_otp_code(page, code)
                            logger.info("Test login %s: OTP code entered via fallback path", retailer_name)
                            await wait_for_page_ready(page, timeout=10000)
                            # Re-check if we navigated away from login
                            final_url2 = page.url
                            still_on_login2 = "/login" in final_url2 or "/signin" in final_url2 or "/sign-in" in final_url2 or "/identity" in final_url2
                            if not still_on_login2:
                                logger.info("Test login successful for %s user=%s after OTP (navigated to %s)", retailer_name, email, final_url2)
                                try:
                                    browser_cookies = await context.cookies()
                                    cookies_dict = {c["name"]: c["value"] for c in browser_cookies if c.get("name") and c.get("value")}
                                    if cookies_dict:
                                        import json as _json
                                        db.set_retailer_session(
                                            user["id"], retailer,
                                            cookies_json=_json.dumps(cookies_dict),
                                        )
                                        if engine.checkout_engine:
                                            engine.checkout_engine._api.load_session_cookies(retailer, cookies_dict)
                                            engine.checkout_engine._api.reset_client(retailer)
                                        logger.info("Auto-saved %d session cookies for %s (user %s)", len(cookies_dict), retailer, user["username"])
                                except Exception as cookie_err:
                                    logger.warning("Failed to auto-save cookies for %s: %s", retailer_name, cookie_err)
                                return {"ok": True, "message": f"{retailer_name} login successful — session cookies saved for API checkout"}
                            else:
                                return {"ok": False, "message": f"{retailer_name} login: OTP code entered but still on login page"}
                        else:
                            db.expire_otp_request(otp_id)
                            return {"ok": False, "message": f"{retailer_name} verification code was not entered within 5 minutes."}

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
        spend_limit = data.get("spend_limit")
        if spend_limit is not None:
            spend_limit = max(0, float(spend_limit))
        db.update_user_settings(
            user["id"],
            poll_interval=data.get("poll_interval"),
            discord_webhook=data.get("discord_webhook"),
            spend_limit=spend_limit,
        )
        return {"ok": True}

    @app.post("/api/settings/generate_api_key")
    async def api_generate_key(user: dict = Depends(get_current_user)):
        """Generate a new API key for the current user (replaces any existing key)."""
        key = db.generate_api_key(user["id"])
        return {"ok": True, "api_key": key}

    # --- Error log ---

    @app.get("/api/errors")
    async def api_errors(user: dict = Depends(get_current_user)):
        errors = db.get_error_log(user["id"], limit=100)
        for e in errors:
            _fix_utc_timestamps(e, "created_at")
        return {"errors": errors}

    # --- Admin endpoints ---

    def require_admin(user: dict = Depends(get_current_user)) -> dict:
        if not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="Admin access required")
        return user

    @app.get("/api/admin/users")
    async def api_admin_users(user: dict = Depends(require_admin)):
        users = db.get_all_users()
        for u in users:
            _fix_utc_timestamps(u, "created_at", "last_login")
        return {"users": users}

    @app.get("/api/admin/pending")
    async def api_admin_pending(user: dict = Depends(require_admin)):
        pending = db.get_pending_users()
        for p in pending:
            _fix_utc_timestamps(p, "created_at")
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

    # --- Session / Cookie import (for API-first checkout) ---

    @app.get("/api/sessions")
    async def api_get_sessions(user: dict = Depends(get_current_user)):
        """List which retailers have imported session cookies."""
        result = {}
        for retailer in ("target", "walmart", "bestbuy", "pokemoncenter", "costco"):
            session = db.get_retailer_session(user["id"], retailer)
            if session:
                import json as _json
                try:
                    cookies = _json.loads(session["cookies_json"])
                    updated = session["updated_at"]
                    if updated and not updated.endswith(("Z", "+00:00")):
                        updated = updated.replace(" ", "T") + "Z"
                    result[retailer] = {
                        "has_session": True,
                        "cookie_count": len(cookies),
                        "updated_at": updated,
                    }
                except Exception:
                    result[retailer] = {"has_session": False}
            else:
                result[retailer] = {"has_session": False}
        return {"sessions": result}

    @app.post("/api/sessions/import")
    async def api_import_session(request: Request, user: dict = Depends(get_current_user)):
        """Import session cookies for a retailer.

        Accepts cookies in multiple formats:
        1. Array of {name, value, domain, path} objects (browser export format)
        2. Object of {name: value} pairs (simple format)
        3. Raw cookie header string ("name1=val1; name2=val2")
        """
        import json as _json

        data = await request.json()
        retailer = data.get("retailer", "").strip()
        cookies_raw = data.get("cookies")

        if retailer not in ("target", "walmart", "bestbuy", "pokemoncenter", "costco"):
            return JSONResponse({"error": "Invalid retailer"}, 400)

        if not cookies_raw:
            return JSONResponse({"error": "No cookies provided"}, 400)

        # Normalize cookies to {name: value} dict
        cookies_dict = {}
        if isinstance(cookies_raw, list):
            # Browser extension format: [{name, value, domain, ...}]
            for c in cookies_raw:
                if isinstance(c, dict) and "name" in c and "value" in c:
                    cookies_dict[c["name"]] = c["value"]
        elif isinstance(cookies_raw, dict):
            # Simple {name: value} format
            cookies_dict = {str(k): str(v) for k, v in cookies_raw.items()}
        elif isinstance(cookies_raw, str):
            # Raw cookie header string: "name1=val1; name2=val2"
            for pair in cookies_raw.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    name, _, value = pair.partition("=")
                    cookies_dict[name.strip()] = value.strip()

        if not cookies_dict:
            return JSONResponse({"error": "Could not parse cookies"}, 400)

        # Store in database
        db.set_retailer_session(
            user["id"], retailer,
            cookies_json=_json.dumps(cookies_dict),
        )

        # If checkout engine exists, hot-reload the session
        if engine.checkout_engine:
            engine.checkout_engine._api.load_session_cookies(retailer, cookies_dict)
            engine.checkout_engine._api.reset_client(retailer)

        logger.info("Imported %d cookies for %s (user %s)", len(cookies_dict), retailer, user["username"])
        return {"ok": True, "cookie_count": len(cookies_dict)}

    @app.delete("/api/sessions/{retailer}")
    async def api_delete_session(retailer: str, user: dict = Depends(get_current_user)):
        if retailer not in ("target", "walmart", "bestbuy", "pokemoncenter", "costco"):
            return JSONResponse({"error": "Invalid retailer"}, 400)
        db.delete_retailer_session(user["id"], retailer)
        return {"ok": True}

    # --- OTP relay ---

    @app.get("/api/otp")
    async def api_get_otp(user: dict = Depends(get_current_user)):
        """Get the current pending OTP request (if any) for this user."""
        pending = db.get_pending_otp(user["id"])
        if pending:
            _fix_utc_timestamps(pending, "created_at")
        return {"otp": pending}

    @app.post("/api/otp/submit")
    async def api_submit_otp(request: Request):
        """Submit an OTP code. Supports two modes:
        1. API key (phone shortcut) — POST /api/otp/submit?key=API_KEY&code=123456
        2. JWT (dashboard UI)      — POST /api/otp/submit  body: {code: "123456"}
        """
        code = request.query_params.get("code", "").strip()
        api_key = request.query_params.get("key", "").strip()
        otp_id = None

        if api_key:
            # Mode 1: API key from phone shortcut
            user = db.get_user_by_api_key(api_key)
            if not user:
                return JSONResponse({"error": "Invalid API key"}, 401)
            pending = db.get_pending_otp(user["id"])
            if pending:
                otp_id = pending["id"]
        else:
            # Mode 2: JWT auth from header
            try:
                user = get_current_user(request)
            except HTTPException:
                return JSONResponse({"error": "Authentication required (API key or JWT)"}, 401)
            try:
                body = await request.json()
            except Exception:
                body = {}
            code = code or body.get("code", "").strip()
            otp_id = body.get("otp_id")
            # Auto-find pending OTP if otp_id not provided
            if not otp_id:
                pending = db.get_pending_otp(user["id"])
                if pending:
                    otp_id = pending["id"]

        if not code:
            return JSONResponse({"error": "code is required"}, 400)

        # Strip spaces/dashes from code
        code = code.replace(" ", "").replace("-", "")

        if not otp_id:
            # No pending request yet — store for when the engine creates one
            db.store_presubmitted_otp(user["id"], code)
            logger.info("OTP code pre-submitted for user %d (no pending request yet)", user["id"])
            return {"ok": True, "code": code, "message": "Code stored — it will be used when the login is ready."}

        ok = db.submit_otp_code(int(otp_id), code)
        if not ok:
            return JSONResponse({"error": "OTP request not found or already resolved"}, 404)

        logger.info("OTP code submitted for request %s", otp_id)
        return {"ok": True, "code": code, "message": "Code received, entering it now..."}

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
