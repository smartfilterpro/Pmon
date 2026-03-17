"""Auto-checkout engine: API-first with optional browser fallback.

AUDIT SUMMARY (2026-03-17) — ARCHITECTURE OVERVIEW:
=============================================================================
The checkout system has two layers:

  1. ApiCheckout (api_checkout.py) — Direct HTTP calls to Target's internal
     APIs (carts.target.com, gsp.target.com, api.target.com). This is the
     "fast path" that works headlessly on cloud deployments. It requires
     session cookies imported from a real browser (via Dashboard > Import
     Cookies). The GSP OAuth login is unreliable due to PerimeterX.

  2. CheckoutEngine (engine.py) — Playwright browser automation as fallback.
     This is the "browser path" that launches headless Chromium, navigates
     the actual Target website, and clicks through the checkout flow. This
     path is BROKEN due to Target site changes introducing new popups/modals
     that the bot doesn't handle.

WHAT BROKE:
  - Target added new interstitials/modals during checkout (likely: delivery
    method selection modals, "sign in for deals" prompts, cookie consent
    changes, store picker sheets, age verification gates for certain products)
  - The _dismiss_target_overlay() method only handles cookie/privacy overlays
  - There is NO universal popup detection/dismissal mechanism
  - The checkout flow is linear with no recovery — any unexpected element
    causes a silent failure or crash

WHAT NEEDS TO BE BUILT:
  - PopupHandler class that sweeps for ANY modal/dialog/overlay after every
    major action, using both CSS selectors and Claude vision as fallback
  - Each checkout step wrapped in its own retry-capable async method
  - Price guard before placing order (needs max_price in config)
  - Screenshot logging at every step for debugging
  - The existing stealth JS, session persistence, and vision helpers are
    good foundations to build on

DEPENDENCIES AVAILABLE:
  - playwright>=1.40 (in pyproject.toml, NOT in requirements.txt — inconsistency)
  - anthropic>=0.39 (Claude API for vision fallback)
  - httpx[http2]>=0.27 (for API checkout)
  - All other deps are for monitoring/dashboard, not checkout
=============================================================================
"""

from .engine import CheckoutEngine
from .api_checkout import ApiCheckout
