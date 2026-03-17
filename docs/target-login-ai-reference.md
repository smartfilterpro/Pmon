# Target Login AI Reference Guide

## Purpose

This document serves as a comprehensive reference for the AI vision system and
the resilient login state machine. It describes every state, popup, error
condition, and recovery strategy the bot may encounter when logging into
Target.com. The AI vision fallback should use this context to make better
decisions about what it sees on screen.

---

## 1. Target Login Architecture

### 1.1 OAuth Flow Overview

Target uses an **OAuth 2.0 authorization code flow** for web login:

```
1. User navigates to /login
2. Target loads React SPA login form
3. User enters email/phone → clicks "Continue"
4. Target shows auth method picker (password, OTP, etc.)
5. User enters password → clicks "Sign in"
6. Server issues auth code → redirects to:
     /gsp/authentications/v1/auth_codes?client_id=ecom-web-1.0.0
7. Redirect back to homepage with:
     ?code=<uuid>&state=<timestamp>&status=success
8. Client-side JS exchanges code for tokens via:
     /gsp/oauth_validations/v3/token_validations (called TWICE)
9. Session established — user is logged in
```

### 1.2 Login URL

```
https://www.target.com/login?client_id=ecom-web-1.0.0&ui_namespace=ui-default&back_button_action=browser&keep_me_signed_in=true&kmsi_default=true&actions=create_session_request_username
```

Key parameters:
- `client_id=ecom-web-1.0.0` — Required, identifies the web client
- `keep_me_signed_in=true` — Enables persistent session cookies
- `kmsi_default=true` — Pre-checks the "Keep me signed in" checkbox
- `actions=create_session_request_username` — Starts with email/username step

### 1.3 Bot Detection Systems

Target employs multiple layers of bot detection:

| System | Script/Endpoint | What It Does |
|--------|----------------|--------------|
| **PerimeterX (PX)** | `client.px-cloud.net/PXGWPp4wUS/main.min.js` | Primary bot detection. Fingerprints browser, tracks mouse/keyboard entropy, assigns risk score. Collector at `collector-pxgwpp4wus.px-cloud.net/api/v2/collector` |
| **SSX** | `assets.targetimg1.com/ssx/ssx.mod.js` | Session security. Loads with a `seed` parameter that encodes session state |
| **FullStory** | `assets.targetimg1.com/webui/scripts/fullstory/fs.*.js` | Session replay. Records all interactions — can be reviewed for bot patterns |
| **Medallia** | `assets.targetimg1.com/webui/scripts/medallia/embed.prod.*.js` | User experience tracking, detects anomalous behavior |
| **DoubleVerify** | `cdn.doubleverify.com/dvtp_src.js` | Ad fraud detection, but also flags bot traffic |
| **Granify** | `cdn.granify.com/assets/javascript.js` | Conversion optimization AI that detects non-human patterns |

### 1.4 What PerimeterX Measures

PerimeterX (`_px` cookies) evaluates:
- **Mouse movement entropy** — Real humans have varied, curved mouse paths. Bots move in straight lines or don't move at all.
- **Keyboard timing** — Uniform `delay=40` is suspicious. Real typing has variable inter-key delays (30-120ms).
- **Scroll behavior** — Real users scroll. Bots that never scroll are flagged.
- **Time on page** — Jumping through pages in < 1 second is not human.
- **Navigation pattern** — Going directly to `/login` without visiting the homepage first is suspicious.
- **Canvas/WebGL fingerprint** — Must match a real browser. Our STEALTH_JS handles this.
- **Focus/blur events** — Tab switching, window focus changes.
- **Touch vs mouse** — Consistency between claimed device and input method.

---

## 2. Login Flow States

The login process is modeled as a **state machine** with the following states:

```
                    +-----------+
                    | HOMEPAGE  |  (warm-up visit)
                    +-----+-----+
                          |
                    +-----v-----+
                    | LOGIN_PAGE|  (navigate to /login)
                    +-----+-----+
                          |
                    +-----v------+
               +--->| EMAIL_ENTRY|  (type email/phone)
               |    +-----+------+
               |          |
               |    +-----v----------+
               |    | EMAIL_SUBMITTED|  (clicked Continue)
               |    +-----+----------+
               |          |
               |    +-----v-----------+
               |    | AUTH_METHOD_PICK|  (choose password vs OTP)
               |    +-----+-----------+
               |          |
               |    +-----v---------+
               |    | PASSWORD_ENTRY|  (type password)
               |    +-----+---------+
               |          |
               |    +-----v-----------+
               |    | SIGN_IN_CLICKED|  (clicked Sign In)
               |    +-----+-----------+
               |          |
               |    +-----v-----------+
               |    | TOKEN_EXCHANGE |  (OAuth redirect + validation)
               |    +-----+-----------+
               |          |
               |    +-----v--------+
               |    | LOGGED_IN    |  (success - on homepage with session)
               |    +--------------+
               |
               |    +----------+
               +----|  ERROR   |  (any failure → diagnose & retry)
                    +----------+
```

### 2.1 State: HOMEPAGE (Warm-up)

**Purpose:** Establish cookies and a normal-looking session before hitting /login.

**What to do:**
1. Navigate to `https://www.target.com/`
2. Wait for `networkidle` (no requests for 500ms)
3. Random mouse movement across the page (3-5 moves)
4. Random scroll down 200-400px, pause, scroll back up
5. Wait 2-4 seconds (randomized)
6. Dismiss any cookie/privacy overlay if present

**What can go wrong:**
- PerimeterX challenge page (rare on homepage)
- Blank page (network issue or IP block)
- "Technical difficulties" error page

**AI Vision Prompt (if needed):**
> "Is this the Target.com homepage? I should see the Target logo, search bar, and product listings. If I see a CAPTCHA, error message, or blank page, describe what you see."

### 2.2 State: LOGIN_PAGE

**Purpose:** Navigate to the login form and wait for it to render.

**What to do:**
1. Navigate to the full login URL (see section 1.2)
2. Wait for `load` event (React SPA needs full load, not just DOM)
3. Poll for email input field every 500ms for up to 15 seconds
4. If field doesn't appear after 5 seconds, dismiss overlays and check again
5. If field doesn't appear after 15 seconds, reload page (up to 3 times)

**Critical:** Target's login is a React SPA. `domcontentloaded` fires before
React hydrates and renders the form. Always use `load` and then poll for the
actual form element.

**What can go wrong:**
- Overlay/modal blocks interaction (see Section 3)
- React doesn't hydrate (blank form area) → reload
- PerimeterX challenge page
- Redirect away from /login (already signed in from cookies)

**AI Vision Prompt (if form not found):**
> "I'm on Target's login page. Do you see an email/username input field? If yes, describe its location. If you see a popup, modal, overlay, CAPTCHA, or error instead, describe exactly what you see."

### 2.3 State: EMAIL_ENTRY

**Purpose:** Enter the user's email or phone number into the username field.

**Selectors (in priority order):**
```
#username
input[name="username"]
input[type="email"]
input[type="tel"]
input[id*="username" i]
input[name*="email" i]
input[autocomplete="username"]
input[autocomplete="email tel"]
```

**What to do:**
1. Click the email field (`force=True` to bypass any remaining overlay)
2. Small pause (200-400ms, randomized)
3. Select all existing text (`Ctrl+A`)
4. Type email with **variable delay** (30-80ms per character, not fixed 40ms)
5. Pause 300-600ms after typing
6. **Verify** the input value matches what was typed (Target's JS can clear it)
7. If verification fails, use `fill()` as fallback
8. If fill() also fails, triple-click + retype

**Human-like behavior:**
- Move mouse to the input field before clicking (not instant teleport)
- Slight pause before starting to type
- Variable typing speed (faster for common letter sequences, slower for special chars)

**What can go wrong:**
- Target's JS clears the field after typing (reactive form validation)
- Overlay intercepts the click
- Field is inside an iframe (hasn't been observed, but check)

### 2.4 State: EMAIL_SUBMITTED

**Purpose:** Click "Continue with email" / "Continue" to submit the email.

**Button selectors:**
```
button:has-text("Continue with email")
button:has-text("Continue")
button:has-text("Sign in")
button:has-text("Next")
button[type="submit"]
```

**Critical: Wait for button to be ENABLED before clicking.**

Target often disables the submit button while validating the email format.
The button appears grayed out / non-interactive. Signs of a disabled button:
- `disabled` attribute on the `<button>` element
- `aria-disabled="true"` attribute
- CSS class containing "disabled" or opacity < 1
- Cursor style is "not-allowed" or "default" instead of "pointer"

**What to do:**
1. Check if button has `disabled` attribute
2. If disabled, wait with polling:
   - Check every 500ms for up to 30 seconds
   - Use `page.wait_for_function()`:
     ```js
     () => {
       const btn = document.querySelector('button[type="submit"]');
       return btn && !btn.disabled && !btn.getAttribute('aria-disabled');
     }
     ```
3. Once enabled, move mouse to button, pause 200-400ms, then click
4. After click, wait for navigation or page change (up to 10 seconds)

**What can go wrong:**
- Button stays grayed out indefinitely (email format not accepted)
- Multiple "Continue" buttons visible (one in overlay, one in form)
- Page shows error below email field ("Please enter a valid email")
- CAPTCHA appears between email and password steps

**AI Vision Prompt (if button seems stuck):**
> "I see a login form with an email entered. Is the Continue/Submit button enabled or grayed out/disabled? Is there an error message below the email field? Is there a CAPTCHA or popup blocking the button?"

### 2.5 State: AUTH_METHOD_PICK

**Purpose:** Target shows an auth method picker after email submission. Choose "Password".

**What you'll see:**
- A list of options like:
  - "Enter your password"
  - "Get a verification code by email"
  - "Get a verification code by text"
- Options may appear as buttons, radio buttons, or clickable div cards

**Selectors (in priority order):**
```
# Button-style
button:has-text("Enter your password")
button:has-text("Enter password")
button:has-text("Password")
button:has-text("Use password")

# Radio-style
input[type="radio"][value*="password" i]
label:has-text("Password")

# Generic clickable elements
div:has-text("Enter your password")
a:has-text("password")
[data-test*="password" i]
```

**What to do:**
1. Wait up to 5 seconds for the picker to appear
2. If picker appears, click "Enter your password" or equivalent
3. If no picker appears, password field may already be visible (single-step login for some accounts)
4. After clicking, wait for password field to appear (up to 10 seconds)

**What can go wrong:**
- Picker doesn't appear (skip to PASSWORD_ENTRY)
- Only OTP options shown (no password option) — need to handle gracefully
- Additional security prompt ("We don't recognize this device")
- Target asks for phone number verification first

**AI Vision Prompt:**
> "I see an authentication method selection page. What options are available? I need to find and click the 'Password' or 'Enter your password' option. If you see radio buttons, buttons, or clickable cards with auth methods, describe their locations."

### 2.6 State: PASSWORD_ENTRY

**Purpose:** Enter the password and prepare to submit.

**Selectors:**
```
#password
input[name="password"]
input[type="password"]
input[id*="password" i]
```

**What to do:**
1. Wait for password field to be visible and enabled (up to 10 seconds)
2. Click into the field (with mouse movement)
3. Type password with **variable delay** (30-80ms per character)
4. Pause 300-600ms
5. Verify the field has content (can't read password value, but can check `input_value` length)
6. Check "Keep me signed in" checkbox if not already checked

**"Keep me signed in" checkbox selectors:**
```
input[name="keepMeSignedIn"]
input[id*="keepMe" i]
input[type="checkbox"][name*="remember" i]
label:has-text("Keep me signed in")
```

### 2.7 State: SIGN_IN_CLICKED

**Purpose:** Click the "Sign in" button and handle the result.

**Button selectors:**
```
button:has-text("Sign in")
button:has-text("Log in")
button[type="submit"]
```

**Critical: Same grayed-out button issue as EMAIL_SUBMITTED.**

**What to do:**
1. **Wait for the Sign in button to be enabled** (same polling as 2.4)
2. Move mouse to button, pause, click
3. After clicking, wait for ONE of these outcomes (up to 15 seconds):
   a. URL changes away from /login → likely success → go to TOKEN_EXCHANGE
   b. Password field shows error → wrong password → go to ERROR
   c. CAPTCHA appears → go to ERROR
   d. "Something went wrong" error → go to ERROR
   e. Page reloads → may need to re-enter credentials

**What can go wrong:**
- Wrong password error
- Account locked ("too many attempts")
- CAPTCHA / reCAPTCHA challenge
- 2FA prompt (SMS/email code)
- "We don't recognize this device" challenge
- Network timeout during OAuth redirect
- Button stays disabled (password validation issue)

**AI Vision Prompt (after clicking sign in):**
> "I just clicked Sign In on Target's login page. What happened? Do you see: (a) the Target homepage (success), (b) an error message, (c) a CAPTCHA, (d) a 2FA/verification code prompt, (e) still on the login page, or (f) something else? Describe what you see."

### 2.8 State: TOKEN_EXCHANGE

**Purpose:** Wait for the OAuth redirect and token validation to complete.

**What happens (invisible to user):**
1. Browser redirects to auth_codes endpoint
2. Redirect back to `/?code=<uuid>&state=<timestamp>&status=success`
3. Client JS calls `token_validations` endpoint (twice)
4. Cookies are set (session established)

**What to do:**
1. After sign-in click, wait for URL to change from /login
2. Wait for `networkidle` (the token validation calls must complete)
3. Check that we're on the homepage or wherever the redirect lands
4. Wait an additional 2-3 seconds for session cookies to be fully set
5. **Save browser context/cookies** for future reuse

**What can go wrong:**
- Redirect loop (bad state/code)
- Token validation fails (expired session)
- Blank page after redirect
- "Technical difficulties" error

### 2.9 State: LOGGED_IN (Success)

**Verification that login succeeded:**
1. URL is `https://www.target.com/` (or another non-login page)
2. Account icon shows user's name or initials (not "Sign in")
3. API call to `guest_profile_details` returns valid data

**Selectors for "signed in" indicators:**
```
#account
[data-test="accountNav"]
[data-test="@web/AccountLink"]
a[href*="/account"]
```

**AI Vision Prompt (to verify success):**
> "I should be logged into Target.com. Do you see a user account icon (not a generic 'Sign in' link) in the top-right area? Is there any indication the user is signed in?"

---

## 3. Popups, Modals, and Overlays

Target shows various popups that can appear at ANY point during the login flow.
The bot must check for and dismiss these **before every major action**.

### 3.1 Cookie/Privacy Consent Overlay

**When:** First visit, or after clearing cookies.

**Appearance:** Full-width banner at bottom or floating modal asking to accept cookies.

**Selectors:**
```
[data-floating-ui-portal] button:has-text("Accept")
[data-floating-ui-portal] button:has-text("Close")
[data-floating-ui-portal] button:has-text("Got it")
#onetrust-accept-btn-handler
button[id*="accept" i]
button[id*="cookie" i]
```

**Dismiss strategy:** Click "Accept" or "Got it" button.

**JS fallback (if button click doesn't work):**
```js
document.querySelectorAll('[data-floating-ui-portal]').forEach(el => el.remove());
document.querySelectorAll('[data-floating-ui-inert]').forEach(el => {
    el.removeAttribute('data-floating-ui-inert');
    el.removeAttribute('aria-hidden');
});
```

### 3.2 "Sign in for the best experience" Modal

**When:** Homepage visit, before navigating to login.

**Appearance:** Centered modal with a prompt to sign in for deals/personalized experience.

**Selectors:**
```
[role="dialog"] button:has-text("Close")
[role="dialog"] button:has-text("Not now")
[role="dialog"] button[aria-label="close"]
[aria-modal="true"] button:has-text("Close")
button[aria-label="close dialog"]
```

### 3.3 Location/Store Picker

**When:** First visit or when location isn't set.

**Appearance:** "Choose your store" or "Update your location" sheet/modal.

**Selectors:**
```
button:has-text("Skip")
button:has-text("Not now")
button:has-text("Close")
[data-test="storePickerClose"]
[aria-label="close store picker"]
```

### 3.4 Age Gate

**When:** Navigating to age-restricted product pages (alcohol, etc.)

**Appearance:** "Are you 21 or older?" modal.

**Selectors:**
```
button:has-text("Yes")
button:has-text("I am 21")
[data-test="ageGateConfirm"]
```

### 3.5 Health Data Consent Modal

**When:** Health-related products (supplements, vitamins, health monitors, Pokemon
cards with health supplements, etc.). Appears on PRODUCT PAGE load, BEFORE
add-to-cart can be clicked. Also can appear after add-to-cart on some products.

**Appearance:** Modal dialog titled "Health Data Consent" informing users that
"some information collected may be health data under certain state laws" and
requiring agreement to "Terms and Health Privacy Policy". Blocks ALL page
interaction until acknowledged.

**CRITICAL:** Must be dismissed BEFORE attempting add-to-cart. The checkout
engine calls `_dismiss_health_consent_modal()` explicitly on the product page
and also on retry if add-to-cart fails.

**Selectors (implemented in both sweep_popups and _dismiss_health_consent_modal):**
```
button[data-test="healthFlagModalAcceptButton"]
button:has-text("I understand")
button:has-text("I accept")
button:has-text("Accept")
button:has-text("I agree")
[role="dialog"] button:has-text("confirm")
[role="dialog"] button:has-text("agree")
[role="dialog"] button:has-text("I agree")
[role="dialog"] button:has-text("Agree")
[role="dialog"] button:has-text("Accept")
[role="dialog"] button:has-text("Continue")
[aria-modal="true"] button:has-text("I agree")
[aria-modal="true"] button:has-text("Agree")
[aria-modal="true"] button:has-text("I understand")
[aria-modal="true"] button:has-text("Accept")
dialog button:has-text("I agree")
dialog button:has-text("Agree")
dialog button:has-text("I understand")
```

### 3.6 CAPTCHA / PerimeterX Challenge

**When:** After PerimeterX flags the session as suspicious.

**Appearance:** "Press & Hold" button, or reCAPTCHA checkbox, or an interstitial page.

**Detection:**
```
iframe[src*="captcha"]
iframe[src*="recaptcha"]
iframe[src*="perimeterx"]
#px-captcha
[class*="captcha" i]
text="Press & Hold"
text="Verify you are human"
```

**Recovery strategy:**
- This is NOT automatically solvable
- Log the event, save screenshot
- Notify user via Discord/console
- If session cookies from a real browser are available, switch to those
- Consider falling back to API-only flow with fresh cookies

### 3.7 "Delivery to [ZIP]" Prompt

**When:** After adding to cart or during checkout.

**Appearance:** Asks to confirm delivery ZIP code.

**Selectors:**
```
button:has-text("Confirm")
button:has-text("Update")
button:has-text("Save")
input[name="zipCode"]
```

### 3.8 Generic Modal/Dialog Detection

For ANY unrecognized popup, use these generic selectors:

```
[role="dialog"]
[aria-modal="true"]
[data-floating-ui-portal]
.ReactModal__Overlay
.modal-overlay
div[class*="Modal" i][class*="overlay" i]
```

**Dismiss strategies (in order):**
1. Look for close/dismiss button inside the modal
2. Press Escape key
3. Click outside the modal (on the overlay backdrop)
4. Remove via JS as last resort

**Universal Popup Sweep function should:**
1. Check all generic modal selectors
2. If found, screenshot + ask AI "What is this popup? How should I dismiss it?"
3. Try AI-suggested action
4. Verify popup is gone
5. Return to the interrupted flow

---

## 4. Human-Like Behavior Patterns

### 4.1 Mouse Movement

PerimeterX tracks mouse movement entropy. The bot must generate realistic patterns.

**Implementation:**
```python
async def human_mouse_move(page, target_x, target_y, steps=None):
    """Move mouse from current position to target with a curved, human-like path."""
    if steps is None:
        # More steps for longer distances
        distance = ((target_x - current_x)**2 + (target_y - current_y)**2) ** 0.5
        steps = max(5, int(distance / 50))

    # Use Bezier curve or add random noise to intermediate points
    # Don't move in a perfectly straight line
    for i in range(steps):
        progress = (i + 1) / steps
        # Add slight randomness (±5px) to intermediate points
        noise_x = random.randint(-5, 5) if i < steps - 1 else 0
        noise_y = random.randint(-3, 3) if i < steps - 1 else 0
        x = int(current_x + (target_x - current_x) * progress + noise_x)
        y = int(current_y + (target_y - current_y) * progress + noise_y)
        await page.mouse.move(x, y)
        await page.wait_for_timeout(random.randint(5, 20))
```

### 4.2 Typing Patterns

**Don't:** Uniform delay between every keypress.
**Do:** Variable timing that mimics real typing:

```python
async def human_type(page, text, wpm=None):
    """Type text with human-like variable delays."""
    if wpm is None:
        wpm = random.randint(40, 65)  # Words per minute

    base_delay = 60000 / (wpm * 5)  # ms per character

    for i, char in enumerate(text):
        # Special characters take longer
        if char in '@._-!#$%':
            delay = base_delay * random.uniform(1.5, 2.5)
        # Same finger/hand transitions are faster
        elif i > 0 and char == text[i-1]:
            delay = base_delay * random.uniform(0.4, 0.7)
        # Capital letters (shift key) take longer
        elif char.isupper():
            delay = base_delay * random.uniform(1.2, 1.8)
        else:
            delay = base_delay * random.uniform(0.7, 1.3)

        await page.keyboard.type(char, delay=0)
        await page.wait_for_timeout(int(delay))
```

### 4.3 Random Scroll

```python
async def idle_scroll(page):
    """Scroll down and back up like a human glancing at the page."""
    scroll_amount = random.randint(150, 400)
    await page.mouse.wheel(0, scroll_amount)
    await page.wait_for_timeout(random.randint(500, 1500))
    await page.mouse.wheel(0, -scroll_amount // 2)
    await page.wait_for_timeout(random.randint(300, 800))
```

### 4.4 Click Patterns

```python
async def human_click(page, x, y):
    """Click with human-like behavior: move to target, small pause, click."""
    await human_mouse_move(page, x, y)
    await page.wait_for_timeout(random.randint(100, 300))  # Dwell time
    await page.mouse.click(x, y)
    await page.wait_for_timeout(random.randint(200, 500))  # Post-click pause
```

### 4.5 Page Load Wait Strategy

**Never use fixed waits. Use adaptive waits:**

```python
async def wait_for_page_ready(page, timeout=30000):
    """Wait for page to be interactive, not just loaded."""
    # 1. Wait for network to settle
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except:
        pass  # Some pages never reach networkidle due to polling

    # 2. Wait for no pending XHR/fetch requests
    await page.wait_for_function("""
        () => {
            // Check if there are pending requests (PerformanceObserver)
            const entries = performance.getEntriesByType('resource');
            const recent = entries.filter(e =>
                e.startTime > performance.now() - 1000
            );
            return recent.length === 0;
        }
    """, timeout=10000).catch(() => None)

    # 3. Small random delay (human reading time)
    await page.wait_for_timeout(random.randint(500, 1500))
```

### 4.6 Wait for Button Enabled

```python
async def wait_for_button_enabled(page, selector, timeout=30000):
    """Wait for a button to become clickable (not disabled/grayed out)."""
    try:
        await page.wait_for_function(f"""
            (selector) => {{
                const btn = document.querySelector(selector);
                if (!btn) return false;
                if (btn.disabled) return false;
                if (btn.getAttribute('aria-disabled') === 'true') return false;
                const style = window.getComputedStyle(btn);
                if (style.pointerEvents === 'none') return false;
                if (parseFloat(style.opacity) < 0.5) return false;
                return true;
            }}
        """, selector, timeout=timeout)
        return True
    except:
        return False
```

---

## 5. Error States and Recovery

### 5.1 Wrong Password

**Detection:**
- Error text below password field: "The password you entered is incorrect"
- `[data-test="error"]`, `[role="alert"]`, `[class*="error" i]`
- AI vision: error message visible

**Recovery:**
- Log the error
- Do NOT retry with the same password (prevent lockout)
- Notify user
- Abort login

### 5.2 Account Locked

**Detection:**
- "Your account has been locked"
- "Too many failed attempts"

**Recovery:**
- Log and notify user immediately
- Do NOT retry
- Suggest user unlock via Target.com directly

### 5.3 CAPTCHA / Bot Block

**Detection:**
- PerimeterX challenge page
- "Please verify you are human"
- "Access denied"
- iframe with CAPTCHA

**Recovery:**
- Save screenshot for debugging
- Notify user
- Try switching to API-only flow with imported browser cookies
- If session cookies available from real browser, reload with those

### 5.4 Network/Timeout Error

**Detection:**
- Navigation timeout
- Blank page
- "Technical difficulties" message

**Recovery:**
- Retry navigation up to 3 times with exponential backoff (2s, 4s, 8s)
- Clear cookies and retry if repeated failures
- Fall back to API-only flow

### 5.5 2FA / Verification Code Prompt

**Detection:**
- "Enter the verification code"
- "We sent a code to your email/phone"
- Input field for 6-digit code

**Recovery:**
- Currently NOT auto-handled
- Save screenshot, notify user
- Future: could integrate with email/SMS code reading

### 5.6 "We don't recognize this device"

**Detection:**
- "Verify your identity"
- "We don't recognize this browser"

**Recovery:**
- Similar to 2FA — requires user intervention
- Notify user, save screenshot
- Suggest importing cookies from an already-authenticated browser session

### 5.7 Session Expired During Checkout

**Detection:**
- Redirect back to /login during checkout
- 401/403 API responses
- "Your session has expired" message

**Recovery:**
- Re-run the full login flow
- Restore cart state after re-login
- Retry checkout

---

## 6. AI Vision Prompt Templates

These are the prompts the bot should use when the AI vision fallback is needed.
They are optimized for accuracy and structured output.

### 6.1 Page State Detection

```
Analyze this screenshot of the Target.com website. Return a JSON object describing
what you see:
{
  "page_type": "homepage|login_form|auth_picker|password_form|captcha|error|block|checkout|unknown",
  "has_popup": true/false,
  "popup_type": "cookie_consent|sign_in_prompt|store_picker|age_gate|captcha|error|unknown|null",
  "popup_dismiss_action": "description of how to dismiss"|null,
  "login_state": "not_started|email_visible|email_filled|password_visible|password_filled|signed_in|error",
  "error_message": "text of any visible error"|null,
  "buttons_visible": ["list", "of", "visible", "button", "texts"],
  "is_button_disabled": true/false (for the primary action button)
}
```

### 6.2 Find Element

```
I need to find the {element_description} on this page. Return ONLY a JSON object:
{"x": N, "y": N, "confidence": "high|medium|low"}.
If the element is not visible, return {"x": null, "y": null, "confidence": "none"}.
If a popup/modal is blocking the element, return {"blocked_by": "description of blocking element"}.
```

### 6.3 Popup Identification

```
There appears to be a popup/modal/overlay on this page. Describe:
{
  "popup_type": "description",
  "dismiss_method": "click_button|press_escape|click_outside|not_dismissable",
  "dismiss_button_text": "text of the dismiss button"|null,
  "dismiss_button_location": {"x": N, "y": N}|null
}
```

### 6.4 Error Diagnosis

```
Something went wrong during the Target login process. Analyze this screenshot and return:
{
  "error_type": "wrong_password|account_locked|captcha|bot_block|network|session_expired|unknown",
  "error_message": "exact text of the error message visible on screen",
  "can_retry": true/false,
  "suggested_action": "description of what to do next"
}
```

---

## 7. API Fallback Flow

When the browser-based login fails (bot blocked, CAPTCHA, etc.), we can
attempt to use previously saved session cookies with the Target API directly.

### 7.1 Key API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `gsp.target.com/gsp/oauth_validations/v3/token_validations` | Validate existing session |
| `api.target.com/guest_profile_details/v1/profile_details/profiles` | Check if logged in |
| `carts.target.com/web_checkouts/v1/cart` | Cart operations |
| `redsky.target.com/redsky_aggregations/v1/web/...` | Product/store data |

### 7.2 Session Cookie Import

The most reliable way to authenticate is to import cookies from a real browser
session. Required cookies:
- `accessToken` — JWT access token
- `refreshToken` — For token renewal
- `idToken` — Identity token
- `GuestLocation` — Store/location data
- `_px*` — PerimeterX cookies (critical for API calls)
- `TealeafAkaSid` — Session ID

### 7.3 When to Switch to API

Switch from browser to API when:
1. PerimeterX blocks the browser session
2. CAPTCHA appears that can't be solved
3. Browser login fails 3 times
4. User has imported fresh session cookies

Switch from API to browser when:
1. API returns 401/403 (session expired)
2. API returns "access denied" or bot detection response
3. Session cookies are stale/missing

---

## 8. Implementation Checklist

### Phase 1: Infrastructure
- [ ] Human-like behavior utilities (mouse, typing, scrolling, waits)
- [ ] Universal popup handler (detect + dismiss any modal)
- [ ] Wait-for-ready helpers (button enabled, network idle, element visible)
- [ ] Screenshot logging for every major step
- [ ] State machine framework

### Phase 2: Login Flow
- [ ] HOMEPAGE warm-up with human behavior
- [ ] LOGIN_PAGE with popup dismissal and form detection
- [ ] EMAIL_ENTRY with verification and retry
- [ ] EMAIL_SUBMITTED with button-enabled wait
- [ ] AUTH_METHOD_PICK with fallback strategies
- [ ] PASSWORD_ENTRY with human typing
- [ ] SIGN_IN_CLICKED with button-enabled wait
- [ ] TOKEN_EXCHANGE with network idle wait
- [ ] LOGGED_IN verification

### Phase 3: Error Handling
- [ ] Wrong password detection and abort
- [ ] CAPTCHA detection and user notification
- [ ] Bot block detection and session rotation
- [ ] Network error retry with backoff
- [ ] 2FA detection and user notification

### Phase 4: AI/API Switching
- [ ] Automatic switch to API when browser is blocked
- [ ] Automatic switch to browser when API session expires
- [ ] Session cookie import from real browser
- [ ] Cookie health check before attempting API calls

---

## 9. Configuration Options

New config fields needed:

```yaml
login:
  # Human-like behavior
  typing_wpm_range: [40, 65]      # Words per minute range
  mouse_movement: true             # Enable human-like mouse paths
  idle_scroll: true                # Scroll randomly during warm-up

  # Timeouts
  button_enabled_timeout: 30000    # Max wait for grayed-out buttons (ms)
  page_load_timeout: 45000         # Max page load wait (ms)
  element_poll_interval: 500       # How often to check for elements (ms)

  # Retry
  max_login_attempts: 3            # Total login attempts before giving up
  retry_backoff: [2000, 4000, 8000]  # Backoff between retries (ms)

  # Screenshots
  save_debug_screenshots: true     # Save screenshots at each step
  screenshot_dir: ".screenshots"   # Where to save them

  # AI Vision
  vision_model: "claude-sonnet-4-6"
  vision_max_tokens: 1024          # Increased for structured JSON responses
  vision_before_every_click: false # Take screenshot before every click (expensive)

  # Anti-detection
  warm_up_homepage: true           # Visit homepage before login
  warm_up_delay_range: [2000, 5000]  # Random delay during warm-up (ms)
```

---

## 10. Network Request Signatures

Important requests the bot should wait for during login:

### After email submit:
- Wait for XHR to complete (auth method lookup)

### After sign-in click:
```
gsp.target.com/gsp/authentications/v1/auth_codes  (POST)
→ redirects to /?code=...&state=...&status=success
→ triggers:
  gsp.target.com/gsp/oauth_validations/v3/token_validations  (POST, x2)
  api.target.com/guest_profile_details/v1/profile_details/profiles
  carts.target.com/web_checkouts/v1/cart
```

### Network idle after login:
The bot should intercept/observe these requests to know when login is truly
complete, rather than relying on fixed timeouts.

```python
# Example: wait for token validation to complete
async with page.expect_response(
    lambda r: "token_validations" in r.url,
    timeout=15000
) as response_info:
    # ... sign-in click happens here
    pass
response = await response_info.value
if response.status == 200:
    # Token validation succeeded - login complete
```
