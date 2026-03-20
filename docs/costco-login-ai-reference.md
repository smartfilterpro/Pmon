# Costco Login AI Reference Guide

## Purpose

This document serves as a comprehensive reference for the AI vision system and
the resilient login state machine. It describes the login flow, bot detection,
and recovery strategies for Costco.com.

---

## 1. Costco Login Architecture

### 1.1 OAuth Flow Overview

Costco uses a **session-based OAuth login** via IBM WebSphere Commerce:

```
1. User navigates to /LogonForm
2. Costco loads login form (email + password on same page)
3. User enters email + password → clicks "Sign In"
4. POST to /OAuthLogonCmd with form data:
     logonId=<email>&logonPassword=<password>&rememberMe=true
5. On success: redirect to homepage with krypto parameter:
     /?langId=-1&krypto=<encrypted_session_token>
6. Session cookies are set (multiple cookies for auth state)
7. Membership validation occurs server-side
```

### 1.2 Key Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/LogonForm` | Login page (GET) |
| `/OAuthLogonCmd` | OAuth login submission (POST) |
| `/gettoken` | CSRF / session token retrieval |
| `/getemailtoken` | Email verification token |
| `/myaccount` | Account page (session validation) |
| `/AjaxAddOrderItemToShoppingCart` | Add to cart (Ajax) |
| `/OrderItemAdd` | Add to cart (form-based) |
| `/CheckoutCartDisplayView` | Cart/checkout page |
| `/OrderProcessCmd` | Place order |

### 1.3 Product API

Costco uses a **GraphQL API** for product data:

```
POST https://ecom-api.costco.com/ebusiness/product/v1/products/graphql
Content-Type: application/json

{
  "query": "query ProductQuery($itemNumber: String!) { product(itemNumber: $itemNumber) { ... } }",
  "variables": { "itemNumber": "1234567" }
}
```

The API returns inventory status, pricing, and fulfillment availability.

---

## 2. Bot Detection

### 2.1 Akamai Bot Manager

Costco uses **Akamai Bot Manager** (not PerimeterX like Target/Walmart).

Detection signals:
- **mPulse Boomerang**: JavaScript performance monitoring at `s.go-mpulse.net`
- **Sensor data collection**: Client-side JavaScript fingerprinting
- **TLS fingerprinting**: JA3/JA4 hash analysis
- **Behavioral analysis**: Mouse movement, typing patterns, request timing

### 2.2 Queue-it

Costco uses **Queue-it** for high-traffic events (product launches, sales):

```
https://static.queue-it.net/script/queueclient.min.js
https://assets.queue-it.net/costco/integrationconfig/javascript/queueclientConfig.js
```

When Queue-it activates, users are placed in a virtual waiting room before
accessing the product page.

### 2.3 Cookie Consent (OneTrust)

Costco uses **OneTrust** for cookie consent management. The consent banner
must be handled during browser automation.

---

## 3. Authentication Details

### 3.1 Required Cookies

After successful login, these cookies indicate an active session:

| Cookie | Purpose |
|--------|---------|
| `JSESSIONID` | Server session identifier |
| `WC_AUTHENTICATION_*` | WebSphere Commerce auth token |
| `WC_ACTIVEPOINTER` | Active store pointer |
| `WC_USERACTIVITY_*` | User activity tracking |
| `WC_PERSISTENT` | Persistent login state |
| `costco_lang` | Language preference |

### 3.2 Membership Requirement

Costco requires an **active membership** for most online purchases:
- Gold Star, Business, or Executive membership
- Some items (e.g., pharmacy, optical) are available to non-members
- Membership status is validated server-side via the session

### 3.3 Session Cookie Import (Recommended Method)

The most reliable authentication method is importing cookies from a
manually-authenticated browser session:

1. Log into costco.com in a regular browser
2. Export cookies using a browser extension
3. Import via Dashboard > Accounts > Costco > Import Cookies
4. Cookies persist across bot restarts via the database

---

## 4. Product URL Formats

Costco uses several URL patterns for products:

```
# Standard product page
https://www.costco.com/product-name.product.1234567.html

# Short format
https://www.costco.com/p/1234567

# With category path
https://www.costco.com/.product.1234567.html

# Query parameter
https://www.costco.com/ProductDisplay?itemNo=1234567
```

The **item number** (6-8 digits) is the key identifier used across all APIs.

---

## 5. Checkout Flow

### 5.1 API Checkout Steps

```
1. Validate session (/gettoken → check loggedIn status)
2. Get CSRF token (/gettoken → extract token field)
3. Add to cart (POST /AjaxAddOrderItemToShoppingCart with catEntryId)
4. Load checkout (GET /CheckoutCartDisplayView)
5. Apply shipping address (from saved addresses)
6. Apply payment (from saved payment methods)
7. Place order (POST /OrderProcessCmd)
```

### 5.2 Cart API Parameters

```json
{
  "catalogId": "10701",
  "langId": "-1",
  "storeId": "10301",
  "catEntryId": "<item_number>",
  "quantity": "1"
}
```

The `storeId`, `catalogId`, and `langId` values are constants for the
US Costco.com store.

---

## 6. Error Conditions

### 6.1 Common Failures

| Error | Cause | Recovery |
|-------|-------|----------|
| 403 Forbidden | Akamai bot detection | Rotate session, re-import cookies |
| 302 → /LogonForm | Session expired | Re-authenticate or import new cookies |
| "Membership required" | Non-member account | Use a member account |
| Queue-it redirect | High-traffic event | Wait in queue (browser mode) |
| "Item not available" | Out of stock | Monitor will retry next cycle |
| "Quantity exceeded" | Cart limit reached | Reduce quantity |

### 6.2 Rate Limiting

Costco enforces rate limits via Akamai:
- Minimum 5-second interval between requests recommended
- 429 responses trigger exponential backoff (60s → 120s → 240s → 5min)
- Aggressive scraping will result in IP-level blocks

---

## 7. AI Vision Hints

When using Claude Vision fallback for browser-based checkout:

### 7.1 Login Page Elements
- **Email field**: `input[name="logonId"]` or `#logonId`
- **Password field**: `input[name="logonPassword"]` or `#logonPassword`
- **Sign In button**: `input[type="submit"]` or button with "Sign In" text
- **Remember Me**: checkbox near the sign-in button

### 7.2 Product Page Elements
- **Add to Cart**: `input.add-to-cart-btn` or button with "Add to Cart" text
- **Price**: `.price` or `[data-testid="product-price"]`
- **Out of Stock**: Text "Out of Stock" or "Temporarily Unavailable"
- **Quantity selector**: dropdown or input near Add to Cart button

### 7.3 Checkout Page Elements
- **Cart summary**: `.order-summary` section
- **Shipping address**: saved address selector or address form
- **Payment method**: saved payment selector
- **Place Order**: submit button at bottom of checkout flow
- **CVV field**: may appear if payment requires re-verification
