"""Walmart.com selector registry.

REVIEWED [Mission 1] — All Walmart selectors extracted from checkout/engine.py.

VERSION: 2026-03-26
LAST_VALIDATED: 2026-03-26
"""

WALMART_SELECTORS = {
    # --- Product Detail Page (PDP) ---
    "pdp": {
        "add_to_cart": (
            'button[data-tl-id="ProductPrimaryCTA-cta_add_to_cart_button"], '
            'button[data-tl-id*="addToCart"], '
            'button:has-text("Add to cart")'
        ),
    },

    # --- Cart / Checkout ---
    "cart": {
        "checkout": (
            'button[data-tl-id="IPPacCheckOutBtnBottom"], '
            'button:has-text("Check out")'
        ),
    },

    # --- Checkout Flow ---
    "checkout": {
        "place_order": 'button:has-text("Place order")',
        "guest_checkout": (
            'button[data-tl-id="Wel-Guest_cxo_btn"], '
            'button:has-text("Continue without account"), '
            'button:has-text("Guest")'
        ),
    },

    # --- Login / Sign-in ---
    "login": {
        "sign_in": 'button:has-text("Sign in"), a:has-text("Sign in")',
        "email_input": (
            'input[name="email"], input[type="email"], input[id*="email" i], '
            'input[type="tel"], input[name="phone"], input[id*="phone" i], '
            '#phone-number, input[autocomplete="tel"]'
        ),
        "password_input": (
            'input[type="password"], input[name="password"], '
            'input[id*="password" i]'
        ),
        "submit_button": 'button[type="submit"]',
        "submit_texts": ["Continue", "Sign in", "Next"],
        "auth_method_radio": "Password",
    },

    # --- Popup / Overlay ---
    "popup": {},
}
