# Best Buy Login Flow — Complete Walkthrough

This document traces every step of the Best Buy login flow as implemented in `pmon/checkout/engine.py` (`_checkout_bestbuy`, line 2704). The login is embedded within the checkout flow and triggers after the user clicks "Checkout" on the cart page.

---

## Phase 0: Browser Setup (`_get_context`, line 594)

Before anything touches Best Buy, a **stealth Playwright browser context** is created:

1. **Load saved cookies** from `.sessions/bestbuy.json` (if they exist from a prior session)
2. Set **user agent** to a real Chrome string (`Chrome/{version}`)
3. Set **viewport** to 1366x768, screen 1920x1080, locale `en-US`, timezone `America/New_York`
4. Inject **Sec-Ch-Ua headers** to match a real Chrome browser
5. Inject **STEALTH_JS** into every page (removes `navigator.webdriver`, spoofs WebGL, adds canvas noise, emulates `chrome` object) — this evades PerimeterX/DataDome detection

---

## Phase 1: Navigate to Product Page (line 2713)

1. **`page.goto(url)`** — navigates to the Best Buy product URL
   - Waits for `domcontentloaded`
2. **`wait_for_page_ready(page)`** — waits for `networkidle` + no recent resource loads + 400-1000ms human "reading" pause
3. **Check for invitation system** — looks for text matching `/invitation/i` on the page. If found, **aborts immediately** (Best Buy's high-demand items use an invite-only queue)

---

## Phase 2: Human-Like Browsing (lines 2728-2731)

Before touching any buttons, the bot simulates a human browsing the page:

1. **`sweep_popups(page)`** — scans ~25 known popup selectors (cookie consent, "Not now", "Close", age gate, store picker, etc.) and dismisses up to 3 stacked popups with human-like clicks. Falls back to JS removal of stubborn overlays.
2. **`random_mouse_jitter(page)`** — moves the mouse to 2-5 random positions in the viewport along Bezier curves (not straight lines)
3. **`idle_scroll(page)`** — scrolls down 150-400px, pauses 400-1200ms, scrolls back up partially
4. **`random_delay(page, 500, 1500)`** — waits 500-1500ms

---

## Phase 3: Add to Cart (lines 2734-2756)

1. **`sweep_popups(page)`** again (pre-click safety)
2. **BUTTON PUSH: `_smart_click(page, "Add to Cart", ...)`** — multi-strategy click:
   - **Try CSS first**: `button.add-to-cart-button:not([disabled])`, `button.btn-primary.add-to-cart-button`
     - Uses `human_click_element()`: gets bounding box, moves mouse along a Bezier curve to center (with +-3px random offset), dwells 80-250ms, clicks, waits 150-400ms post-click
   - **If CSS fails, Vision fallback**: takes a PNG screenshot, sends it to **Claude Sonnet 4.6** via the Anthropic API:
     - **API call**: `anthropic.messages.create(model="claude-sonnet-4-6", max_tokens=512)`
     - Prompt: _"I need to click the 'Add to Cart' button... Return JSON `{x, y}` coordinates"_
     - Uses returned coordinates with `human_click()` (Bezier mouse movement + dwell + click)
3. If click failed and a popup blocked it, **`sweep_popups(page)`** and retry
4. If still failed, **`_smart_read_error(page)`** — screenshots the page and asks Claude if there's an error visible
5. **`random_delay(page, 1500, 2500)`** — post-add-to-cart wait

---

## Phase 4: Navigate to Cart (lines 2758-2768)

1. **`sweep_popups(page)`** — dismiss protection plan offers, etc.
2. **BUTTON PUSH: `_smart_click(page, "Go to Cart", ...)`**:
   - CSS selectors: `div.go-to-cart-button a`, `a:has-text("Go to Cart")`, `a[href*="/cart"]`
   - Vision fallback if needed
3. If "Go to Cart" button not found: **`page.goto("https://www.bestbuy.com/cart")`** — direct navigation fallback

---

## Phase 5: Click Checkout (lines 2773-2780)

1. **`sweep_popups(page)`**
2. **BUTTON PUSH: `_smart_click(page, "Checkout", ...)`**:
   - CSS: `button[data-track="Checkout - Top"]`, `button:has-text("Checkout")`, `a:has-text("Checkout")`
   - Vision fallback
3. **`wait_for_page_ready(page)`**
4. **`sweep_popups(page)`**

---

## Phase 6: Sign-In — Email Entry (lines 2783-2816)

This is where the **multi-step Best Buy login** begins. Best Buy uses a progressive sign-in form (email first, then method selection, then password).

### Step 6a: Fill Email

1. **`_smart_fill(page, "email", selectors, creds.email)`**:
   - **CSS selectors tried**: `input#fld-e`, `input[id="user.emailAddress"]`, `input[type="email"]`, `input[name="email"]`
   - Waits for field to be visible, then `human_click_element()` on it
   - **`human_type(page, creds.email)`** — types the email character by character at 40-60 WPM with:
     - Variable per-character delays (special chars like `@._` take 1.5-2.5x longer)
     - Occasional 100-300ms "thinking" pauses (3% chance per char)
     - Repeated chars are faster (0.4-0.7x)
   - Vision fallback if CSS fails

### Step 6b: Start Network Monitor (lines 2790-2792)

2. **`NetworkMonitor(page)`** — creates a network request interceptor that watches for:
   - `bb_auth`: `/identity/authenticate` — the primary auth API call
   - `bb_token`: `/oauth/token` — token exchange
   - `bb_signin_options`: `/identity/signin/options` — sign-in option page
   - `bb_account_menu`: `canopy/component/shop/account-menu` — confirms logged-in session
   - `bb_graphql`: `/gateway/graphql` — general GraphQL
   - `bb_recaptcha`: `recaptcha/enterprise` — reCAPTCHA challenges
   - `bb_welcome_back`: `canopy/component/shop/welcome-back-toast` — post-login toast
   - `bb_streams`: `web-streams/v1/events` — telemetry
   - Also adds: `bb_signin_page`: `/identity/signin`
3. **`net_monitor.start()`** — begins intercepting all responses via `page.on("response", ...)`

### Step 6c: Mouse Jitter + Email Verification (lines 2795-2805)

3. **`random_mouse_jitter(page)`** — simulate idle human movement
4. **Verify email was entered correctly**: reads back `input#fld-e` value and compares. If mismatch, uses Playwright `.fill()` as a direct fallback.

### Step 6d: Submit Email (lines 2807-2816)

5. **`wait_for_button_enabled(page, 'button[type="submit"]')`** — polls for up to 10s until the submit button is:
   - Not disabled
   - `aria-disabled` not "true"
   - `pointer-events` not "none"
   - `opacity` >= 0.5
   - `cursor` not "not-allowed"
6. **BUTTON PUSH: `_multi_strategy_click(page, "Continue", ...)`** — **4-strategy button click**:
   - **Strategy 1 (CSS)**: `button[type="submit"]`, `button:has-text("Continue")`, `button:has-text("Sign In")`
   - **Strategy 2 (get_by_role)**: tries "Continue", "Sign In", "Next" as button role names
   - **Strategy 3 (get_by_text)**: same texts but catches links/divs acting as buttons
   - **Strategy 4 (Vision)**: screenshot -> Claude -> click coordinates
   - All use `human_click_element()` with Bezier mouse paths
7. **`wait_for_page_ready(page)`**
8. **`sweep_popups(page)`**

---

## Phase 7: Identity Verification (line 2821) — `_bestbuy_handle_verification()`

After submitting the email, Best Buy may require **identity verification** (phone last 4 digits + last name). This is handled by `_bestbuy_handle_verification()` (line 2423):

1. **Check if verification page appeared** — look for phone/last4 input fields:
   - CSS selectors: `input[id*="phone"]`, `input[name*="last4"]`, `input[id*="lastDigits"]`, `input[name*="phoneLast"]`
   - Checks visibility with 3s timeout

2. **If no phone field found**, check if we can skip:
   - Is `input#fld-p1` (password) already visible? -> skip
   - Is the auth method picker visible (`text=/choose.*sign.?in/i`, `text=/use password/i`)? -> skip
   - Otherwise: **Vision fallback** — screenshot -> Claude API call asking:
     > _"Does this page ask for last 4 digits of phone number and/or last name as identity verification?"_
   - If Claude says yes: fill coordinates via `human_click()` + `human_type()`, then click Submit

3. **If phone field IS found** (selector-based path):
   - **`_smart_fill(page, "phone last 4 digits", ..., phone_last4)`** — types last 4 of phone
   - **`random_delay(page, 300, 600)`**
   - **`_smart_fill(page, "last name", ..., last_name)`** — types account last name
   - **`random_delay(page, 300, 600)`**
   - **`wait_for_button_enabled(page, 'button[type="submit"]')`**
   - **BUTTON PUSH: `_multi_strategy_click(page, "Continue", ["Continue", "Verify", "Submit", "Next"], ...)`**
   - **`wait_for_page_ready(page)`**

---

## Phase 8: Auth Method Picker — Select "Use password" (lines 2823-3060)

Best Buy's login has 3 possible states after email+verification:
- Password field shown directly
- Auth method picker (radio buttons: "Use password" / "One-time code")
- OTP page shown directly (auto-sent a code)

The bot tries to get to the **password** path through 6 escalating strategies:

### Check if password field already visible (lines 2831-2836)
- `input#fld-p1` or `input[type="password"]` visible? -> skip to password entry

### Detect if on OTP page directly (lines 2840-2889)
If on the OTP page (text matches "one-time code", "enter your code", "verification code"):
1. **JS click** to find "Try another way" / "Use password instead" links — searches all `<a>`, `<button>`, `<span[role="button"]>` for text matching ~9 phrases
2. **Vision fallback** for the switch link if JS fails

### Strategy 1: JS click for "Use password" (lines 2898-2959)
Executes a comprehensive `page.evaluate()` script that:
- Scans all `<label>` elements for text like "Use password", "Password", "Sign in with password"
- Scans `<input[type="radio"]>` elements with password-related `value`/`name`/`id`
- Scans all clickable elements for password phrases (excluding "forgot password")
- Scans `data-track`, `data-value`, `data-method` attributes containing "password"
- Clicks the **label** (not the hidden radio) because Best Buy hides radio inputs behind styled labels

### Strategy 2: Playwright label locator (lines 2962-2972)
- Tries `label:has-text("Use password")`, `label:has-text("Password")`, etc. with `human_click_element()`

### Strategy 3: `get_by_label` with force check (lines 2975-2984)
- `page.get_by_label("Use password", exact=False).check(force=True)` — force-checks hidden radio buttons

### Strategy 4: `get_by_text` (lines 2987-2997)
- Exact text matching: "Use password", "Password", "Use a password", "Sign in with password"

### Strategy 5: `get_by_role` (lines 3000-3013)
- Tries roles `radio`, `tab`, `option`, `button`, `link` with password text

### Strategy 6: Vision (lines 3016-3023)
- Screenshot -> Claude: _"The 'Use password' option/radio button/tab to select password-based sign-in"_
- CSS fallback selectors: `label:has-text("password")`, `[role="radio"]:has-text("password")`

### After selecting "Use password":
- Wait up to 5s for `input#fld-p1` or `input[type="password"]` to appear
- If it doesn't appear, run `_bestbuy_handle_verification()` again (verification can appear AFTER selecting the auth method)

### If all 6 strategies fail:
- Dump page diagnostics to logs (all interactive elements, headings, URL, title)
- Check if still on OTP page -> if so, use **OTP relay** as last resort:
  - Creates OTP request in database via `db.create_otp_request()`
  - Sends **Discord webhook** notification asking the user for the code
  - Polls DB for up to 5 minutes waiting for user to submit the code
  - When received, enters the code into the page

---

## Phase 9: Password Entry + Submit (lines 3083-3136)

1. **`_smart_fill(page, "password", selectors, creds.password)`**:
   - CSS: `input#fld-p1`, `input[type="password"]`, `input[name="password"]`
   - `human_click_element()` then `human_type()` (character by character at 40-60 WPM)
   - Vision fallback
2. **Verify password was entered**: read back the input value, use `.fill()` if empty
3. **`wait_for_button_enabled(page, 'button[type="submit"]')`** — wait up to 10s
4. **Record current URL** (`pre_url = page.url`)
5. **BUTTON PUSH: `_multi_strategy_click(page, "Sign In", ["Sign In", "Log In", "Continue"], ...)`**
   - 4-strategy click (CSS -> get_by_role -> get_by_text -> Vision)

---

## Phase 10: Wait for Login Completion (lines 3107-3136)

### Network monitoring (`wait_for_login_complete`, line 3110)

Calls `net_monitor.wait_for_login_complete(timeout=20000, retailer="bestbuy")` which runs `_wait_for_bestbuy_login()` (network_monitor.py, line 269):

**Primary signal** (20s timeout):
- Wait for **`POST /identity/authenticate`** (`bb_auth`) — Best Buy's main auth API call
- If seen, also wait up to 5s for **`POST /oauth/token`** (`bb_token`) — the token exchange

**Fallback signals**:
- If `/identity/authenticate` not seen, check if `bb_token` was captured (>= 1 response)
- If that fails too, check if both `bb_account_menu` AND `bb_welcome_back` were seen — indirect proof of login

### If network signals fail:
- **`wait_for_url_change(page, pre_url)`** — wait up to 10s for the URL to change

### Post-login checks:
- **`net_monitor.was_blocked()`** — check if any tracked requests returned 403/429 (PerimeterX block)
- **`net_monitor.response_count("bb_recaptcha")`** — log if reCAPTCHA Enterprise fired during login
- **Verify login success**: check if `bb_account_menu` or `bb_welcome_back` responses were recorded

---

## Phase 11: Post-Login OTP Check (lines 3138-3156)

Best Buy may require a **one-time code AFTER password submission**:

1. Check for OTP text: "one-time code", "enter your code", "enter the code", "verification code", "enter your one-time" (2s timeout)
2. If OTP detected -> **`_wait_for_otp_code()`**:
   - Creates DB record via `db.create_otp_request(user_id, "bestbuy", context="checkout:{product_name}")`
   - Sends **Discord webhook** with embed including phone shortcut URL, 5-minute expiry
   - **Polls DB** every few seconds for up to 5 minutes
   - When code received -> types it into the page

---

## Phase 12: Post-Login Cleanup (lines 3158-3163)

1. **`sweep_popups(page)`** — dismiss any post-login popups
2. **`_save_context(context, "bestbuy")`** — saves all cookies to `.sessions/bestbuy.json`

---

## API Calls Summary

| # | API Call | Method | Purpose |
|---|---------|--------|---------|
| 1 | `anthropic.messages.create()` | POST | Vision fallback for any click/fill that CSS selectors miss (up to ~8 calls) |
| 2 | `/identity/signin/options` | GET | Best Buy loads sign-in options page (monitored) |
| 3 | `recaptcha/enterprise` | POST | reCAPTCHA Enterprise challenge (monitored, not initiated by bot) |
| 4 | `/identity/authenticate` | POST | **Primary auth call** — submits credentials (monitored) |
| 5 | `/oauth/token` | POST | Token exchange after auth (monitored) |
| 6 | `canopy/component/shop/account-menu` | GET | Post-login account menu load (monitored) |
| 7 | `canopy/component/shop/welcome-back-toast` | GET | Post-login welcome toast (monitored) |
| 8 | `web-streams/v1/events` | POST | Telemetry events (monitored) |
| 9 | Discord webhook | POST | Only if OTP code needed |

## Button Pushes Summary

| # | Button | Method | Primary Selectors |
|---|--------|--------|-----------|
| 1 | **Add to Cart** | `_smart_click` | `button.add-to-cart-button:not([disabled])` |
| 2 | **Go to Cart** | `_smart_click` | `div.go-to-cart-button a`, `a:has-text("Go to Cart")` |
| 3 | **Checkout** | `_smart_click` | `button[data-track="Checkout - Top"]` |
| 4 | **Continue** (email submit) | `_multi_strategy_click` | `button[type="submit"]` |
| 5 | **Continue** (verification) | `_multi_strategy_click` | `button:has-text("Continue")`, `button:has-text("Verify")` |
| 6 | **Use password** (auth picker) | 6-strategy escalation | `label:has-text("Use password")`, radio buttons, vision |
| 7 | **Sign In** (password submit) | `_multi_strategy_click` | `button[type="submit"]`, `button:has-text("Sign In")` |

Every button click uses `human_click_element()` or `human_click()` which moves the mouse along a **cubic Bezier curve** with random jitter, dwells 80-250ms before clicking, and waits 150-400ms after.
