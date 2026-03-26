"""Target.com selector registry.

REVIEWED [Mission 1] — All Target selectors extracted from checkout/engine.py
and checkout/human_behavior.py into this centralized registry.

VERSION: 2026-03-26
LAST_VALIDATED: 2026-03-26
"""

TARGET_SELECTORS = {
    # --- Product Detail Page (PDP) ---
    "pdp": {
        "buy_now": (
            'button:has-text("Buy now"), '
            'button[data-test="buyNowButton"], '
            'a:has-text("Buy now")'
        ),
        "ship_it": (
            'button[data-test="shipItButton"], button[data-test="shippingButton"], '
            'button:has-text("Ship it"), button:has-text("Add to cart")'
        ),
        "add_to_cart_confirm": [
            'button[data-test="addToCartModalViewCartCheckout"]',
            '[data-test="addToCartModal"]',
            'h2:has-text("Added to cart")',
            'div:has-text("Added to cart")',
            'button:has-text("View cart & check out")',
            'button:has-text("View cart")',
        ],
        "decline_coverage": (
            'button[data-test="espModalContent-declineCoverageButton"], '
            'button:has-text("No thanks"), '
            'button:has-text("No, thanks")'
        ),
        "view_cart": (
            'button[data-test="addToCartModalViewCartCheckout"], a[href*="/cart"], '
            'button:has-text("View cart"), button:has-text("View cart & check out")'
        ),
    },

    # --- Cart Page ---
    "cart": {
        "checkout_button": 'button[data-test="checkout-button"]',
        "checkout_all": (
            'button[data-test="checkout-button"], button:has-text("Check out"), '
            'a:has-text("Check out"), button[data-test="checkout-btn"]'
        ),
        "cart_count": '[data-test="cart-count"], [data-test="cartCount"], #cart-count',
        "empty_cart": [
            'h1:has-text("Your cart is empty")',
            '[data-test="emptyCart"]',
            'text="Your cart is empty"',
        ],
        "sign_in_to_checkout": (
            'button[data-test="checkout-sign-in"], '
            'button:has-text("Sign in to check out"), '
            'a:has-text("Sign in to check out")'
        ),
        # Delivery method selection
        "delivery_needed_indicators": [
            'text="Choose a delivery method"',
            'text="Choose delivery method"',
            '[data-test="fulfillment-cell"]',
        ],
        "shipping_options": [
            'button[data-test="fulfillmentOptionShipping"]',
            'button:has-text("Shipping")',
            'button:has-text("Ship")',
            '[data-test="shipping-option"]',
            'label:has-text("Shipping")',
            'div[data-test="fulfillment-cell"] button:first-child',
            'input[type="radio"][value*="SHIP" i]',
            'button:has-text("Standard")',
        ],
        "save_delivery": [
            'button:has-text("Save")',
            'button:has-text("Apply")',
            'button:has-text("Update")',
        ],
        # Pickup options (for shipping minimum workaround)
        "pickup_options": [
            'button:has-text("Change all to pickup")',
            'a:has-text("Change all to pickup")',
            'button:has-text("switch items to pickup")',
            'a:has-text("switch items to pickup")',
            'button:has-text("Switch to pickup")',
            'button:has-text("Pickup")',
            'button:has-text("Order Pickup")',
            'button[data-test="fulfillmentOptionPickup"]',
            '[data-test="pickup-option"]',
        ],
        # Shipping minimum detection
        "shipping_minimum_indicators": [
            ':text("only ship with $35")',
            ':text("only ships with $35")',
            ':text("qualify for shipping")',
            ':text("more to qualify")',
            ':text("$35 minimum")',
            ':text("$35 order")',
        ],
    },

    # --- Checkout Flow ---
    "checkout": {
        "place_order": (
            'button:has-text("Place your order"), '
            'button[data-test="placeOrderButton"], '
            'button:has-text("Place order")'
        ),
        "continue_button": (
            'button[data-test="save-and-continue-button"], '
            'button:has-text("Save and continue"), '
            'button:has-text("Continue"), '
            'button:has-text("Save & continue")'
        ),
        "continue_css": 'button[data-test="save-and-continue-button"]',
        "cvv_inputs": [
            'input[data-test="verify-card-cvv"]',
            'input[name="cvv"]',
            'input[name="cardCvc"]',
            'input[id*="cvv" i]',
            'input[id*="cvc" i]',
            'input[placeholder*="CVV" i]',
            'input[placeholder*="CVC" i]',
            'input[aria-label*="CVV" i]',
            'input[aria-label*="security code" i]',
            'input[autocomplete="cc-csc"]',
        ],
    },

    # --- Login / Sign-in ---
    "login": {
        "email_input": (
            '#username, input[name="username"], input[type="email"], '
            'input[type="tel"], input[id*="username" i], input[name*="email" i], '
            'input[autocomplete="username"], input[autocomplete="email tel"]'
        ),
        "password_input": (
            '#password, input[name="password"], input[type="password"], '
            'input[id*="password" i]'
        ),
        "submit_button": 'button[type="submit"]',
        "account_link": (
            '[data-test="@web/AccountLink"], #account, #accountNav, '
            'a[href*="/account"], a:has-text("Sign in"), '
            'button:has-text("Sign in"), '
            '[data-test="accountNav-signIn"], '
            '[data-test="@web/AccountLink-signIn"]'
        ),
        "sign_in_panel": (
            'a:has-text("Sign in or create account"), '
            'button:has-text("Sign in or create account"), '
            'a[href*="/login"], '
            '[data-test="accountNav-signIn"]'
        ),
        "sign_in_prompts": [
            'button[data-test="checkout-sign-in"]',
            'button:has-text("Sign in to check out")',
            'a:has-text("Sign in to check out")',
            '[data-test="cart-sign-in"]',
            'button:has-text("Sign in to save")',
        ],
        "account_nav": '#account, [data-test="accountNav"]',
        "auth_method_texts": [
            "Enter your password", "Enter password",
            "Password", "Use password", "Sign in with password",
            "password",
        ],
        "auth_method_css": (
            'button:has-text("password"), a:has-text("password"), '
            '[data-test*="password" i], div:has-text("Enter your password"), '
            'label:has-text("Password"), input[type="radio"][value*="password" i], '
            'li:has-text("password"), span:has-text("Enter your password")'
        ),
        "submit_texts": ["Sign in", "Continue", "Log in"],
        "submit_fallback": 'button[type="submit"], button:has-text("Sign in")',
        "login_indicators": ["/login", "/signin", "/sign-in", "/identity"],
    },

    # --- Popup / Overlay Dismissal ---
    "popup": {
        "health_consent": [
            '[data-test="healthFlagModalAcceptButton"]',
            '[data-test="health-consent-modal"] button:has-text("Agree")',
            '[data-test="healthConsentModal"] button:has-text("Agree")',
            'button:has-text("I understand")',
            'button:has-text("I accept")',
            '[role="dialog"] button:has-text("I agree")',
            '[role="dialog"] button:has-text("Agree")',
            '[role="dialog"] button:has-text("Acknowledge")',
            '[role="dialog"] button:has-text("Accept")',
            '[role="dialog"] button:has-text("Continue")',
            '[role="dialog"] button:has-text("confirm")',
            '[role="dialog"] button:has-text("agree")',
            '[aria-modal="true"] button:has-text("I agree")',
            '[aria-modal="true"] button:has-text("Agree")',
            '[aria-modal="true"] button:has-text("I understand")',
            '[aria-modal="true"] button:has-text("Accept")',
            'dialog button:has-text("I agree")',
            'dialog button:has-text("Agree")',
            'dialog button:has-text("Acknowledge")',
            'dialog button:has-text("Accept")',
            'dialog button:has-text("Continue")',
            'dialog button:has-text("I understand")',
            'button:has-text("I agree")',
            'button:has-text("Agree and continue")',
        ],
        "floating_ui_portal": '[data-floating-ui-portal]',
        "floating_ui_inert": '[data-floating-ui-inert]',
    },

    # --- Status Check (is signed in?) ---
    "status": {
        "auth_cookies": ["accessToken", "refreshToken"],
    },
}
