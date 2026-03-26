"""PokemonCenter.com selector registry.

REVIEWED [Mission 1] — All Pokemon Center selectors extracted from checkout/engine.py.

VERSION: 2026-03-26
LAST_VALIDATED: 2026-03-26
"""

POKEMONCENTER_SELECTORS = {
    # --- Product Detail Page (PDP) ---
    "pdp": {
        "add_to_cart": (
            'button:has-text("Add to Cart"), '
            'button:has-text("Add to Bag")'
        ),
    },

    # --- Cart ---
    "cart": {
        "go_to_cart": (
            'a[href*="cart"], a:has-text("Cart"), a:has-text("Bag"), '
            'button:has-text("View Cart"), a:has-text("View Cart")'
        ),
        "checkout": (
            'button:has-text("Checkout"), a:has-text("Checkout"), '
            'button:has-text("Check Out"), a:has-text("Check Out")'
        ),
    },

    # --- Checkout Flow ---
    "checkout": {
        "place_order": (
            'button:has-text("Place Order"), '
            'button:has-text("Submit Order"), '
            'button:has-text("Complete Order")'
        ),
    },

    # --- Login / Sign-in ---
    "login": {
        "email_input": (
            '#login-email, '
            'input[id*="login-email" i], '
            'input[name="email"][type="email"], '
            'input[type="email"], '
            'input[autocomplete="email"], '
            'input[autocomplete="username"]'
        ),
        "password_input": (
            '#login-password, '
            'input[id*="login-password" i], '
            'input[name="password"], '
            'input[type="password"]'
        ),
        "submit_button": (
            'button[type="submit"], '
            'button:has-text("Sign In"), '
            'button:has-text("Log In")'
        ),
        "header_sign_in": (
            'span[class*="header-sign-in" i], '
            '[class*="header-sign-in" i], '
            'a:has-text("Sign In"), '
            'button:has-text("Sign In"), '
            'a[href*="/account/login"], '
            '[class*="sign-in" i], '
            '[data-testid*="sign-in" i], '
            '[data-testid*="signin" i], '
            'a:has-text("Log In"), '
            'button:has-text("Log In")'
        ),
        "submit_texts": ["Sign In", "Log In", "Continue", "Submit"],
        "account_indicators": (
            'a:has-text("My Account"), a[href="/account"], '
            '[class*="account-icon" i], [class*="account-menu" i], '
            'span[class*="header-sign-in" i]:has-text("Hi")'
        ),
        "still_sign_in": (
            'span[class*="header-sign-in" i]:has-text("Sign In"), '
            'a:has-text("Sign In")'
        ),
    },

    # --- Popup / Overlay ---
    "popup": {},
}
