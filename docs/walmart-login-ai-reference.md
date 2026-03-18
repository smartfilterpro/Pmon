# Walmart Login AI Reference Guide

## Purpose

This document serves as a comprehensive reference for the AI vision system and
the resilient login state machine. It describes the OAuth flow, bot detection
layers, API endpoints, and recovery strategies the bot may encounter when
logging into Walmart.com. Derived from live network capture of a Walmart
sign-in session on 2026-03-18.

---

## 1. Walmart Login Architecture

### 1.1 OAuth Flow Overview

Walmart uses an **OAuth 2.0 authorization code flow** for web login:

```
1. User navigates to /account/login (or is redirected there)
2. Walmart loads identity UI (email + password form)
3. User enters credentials → submits
4. Server issues authorization code
5. Redirect to /account/verifyToken with:
     ?state=/orders
     &client_id=5f3fb121-076a-45f6-9587-249f0bc160ff
     &redirect_uri=https://www.walmart.com/account/verifyToken
     &scope=openid+email+offline_access
     &code=<AUTHORIZATION_CODE>
     &action=SignIn
     &rm=true
6. verifyToken endpoint exchanges code for session tokens (server-side)
7. Redirect to final destination:
     /orders?action=SignIn&rm=true
8. Post-login API calls fire:
     - /orchestra/api/ccm/v3/bootstrap (config bootstrap)
     - /orchestra/cph/graphql/accountLandingPage/ (account data)
     - /orchestra/cph/graphql/FetchNotifications/ (notifications)
     - /orchestra/cartxo/graphql/MergeAndGetCart/ (cart merge)
     - /swag/graphql (session/wallet data)
9. Session established — user is logged in
```

### 1.2 OAuth Parameters

Decoded from the verifyToken URL:

| Parameter | Value | Description |
|-----------|-------|-------------|
| `state` | `/orders` | Post-login redirect path |
| `client_id` | `5f3fb121-076a-45f6-9587-249f0bc160ff` | OAuth client identifier for walmart.com web |
| `redirect_uri` | `https://www.walmart.com/account/verifyToken` | Token exchange endpoint |
| `scope` | `openid email offline_access` | OpenID Connect scopes — includes refresh token |
| `code` | `B283E0E18B1745D1BAE891AF8D67A9A7` | One-time authorization code (32-char hex) |
| `action` | `SignIn` | Action type identifier |
| `rm` | `true` | "Remember me" — persistent session |

### 1.3 Key Observations

- **Authorization code format**: 32-character uppercase hexadecimal (e.g., `B283E0E18B1745D1BAE891AF8D67A9A7`)
- **Scope `offline_access`**: Indicates Walmart issues refresh tokens for persistent sessions
- **The `verifyToken` endpoint is both the redirect_uri AND the token exchange endpoint**: Server-side exchange, not client-side
- **Post-login redirect**: Uses the `state` parameter to return user to their intended page

### 1.4 Login URL

The login entry point (inferred from flow):
```
https://www.walmart.com/account/login
```

After successful authentication, the redirect chain is:
```
/account/verifyToken?state=...&code=...  →  /{state}?action=SignIn&rm=true
```

---

## 2. Bot Detection Systems

Walmart employs **extremely aggressive** bot detection — more so than Target.

| System | Script/Endpoint | What It Does |
|--------|----------------|--------------|
| **PerimeterX (PX)** | `client.px-cloud.net/PXu6b0qd2S/main.min.js` | Primary bot detection. App ID: `PXu6b0qd2S`. Fingerprints browser, tracks behavior, assigns risk score. |
| **PX Collector** | `collector-pxu6b0qd2s.px-cloud.net/api/v2/collector` | Telemetry collection endpoint (called repeatedly throughout session) |
| **PX CAPTCHA** | `/px/PXu6b0qd2S/captcha/captcha.js` | "Press & Hold" CAPTCHA challenge. Params: `a=c`, `m=0`, `u=<visitor_id>`, `v=<vid>`, `g=b` |
| **PX Init** | `/px/PXu6b0qd2S/init.js` | PerimeterX initialization script (loaded early) |
| **PX Timezone** | `tzm.px-cloud.net/ns?c=<visitor_id>` | Timezone/geolocation fingerprinting |
| **PX First-party** | `fst-ec.perimeterx.net/?id=<vid>` | First-party cookie enforcement |
| **Device Fingerprinting** | `drfdisvc.walmart.com/*` | Heavy device fingerprinting — multiple requests with encrypted payloads. Custom Walmart system. |
| **Online-Metrix (ThreatMetrix)** | `h.online-metrix.net/*`, `h64.online-metrix.net/*` | LexisNexis ThreatMetrix. Device intelligence, fraud scoring. |
| **RUM Beacon** | `b.www.walmart.com/rum.gif`, `b.www.walmart.com/rum.js` | Real User Monitoring — page load timing, errors, performance |
| **Beacon (Lightest)** | `i5.walmartimages.com/beacon/beacon.js?bd=b.www.walmart.com&bh=beacon.lightest.walmart.com` | Lightweight telemetry beacon |
| **SNR** | `/si/snr.js` | Session/Network recording |
| **Partytown** | `/~partytown/proxytown`, `/~partytown/partytown-sandbox-sw.html` | Web Worker-based third-party script isolation (Builder.io Partytown). Proxies ad/tracking scripts off main thread. |
| **CRCLDU** | `crcldu.com/bd/sync.html`, `crcldu.com/bd/auditor.js` | Cross-domain sync / bot auditing |
| **ID Sync** | `idsync.rlcdn.com/453959.gif?partner_uid=...` | Cross-domain identity sync (LiveRamp) |
| **Ad Viewability** | `s.xlgmedia.com/static/2.179.0/pagespeed.js`, `s.xlgmedia.com/static/2.179.0/main.js` | XL Media ad viewability tracking (called extensively with postback/vw events) |

### 2.1 PerimeterX Details

**App ID**: `PXu6b0qd2S`

**Key Cookies** (from PX):
- `_pxvid` — Visitor ID (persistent, e.g., `ad364660-2305-11f1-a1b7-37517ecae31d`)
- `_px` — Risk assessment token
- `_px3` — Additional risk data
- `_pxhd` — Human detection score
- `_pxde` — Device evaluation

**PX CAPTCHA parameters** (from observed URL):
```
/px/PXu6b0qd2S/captcha/captcha.js?a=c&m=0&u=<_pxvid>&v=<session_vid>&g=b
```
- `a=c` — Action: captcha
- `m=0` — Mode: 0 (standard)
- `u` — Visitor UUID (_pxvid cookie value)
- `v` — Session verification ID
- `g=b` — Group: b

### 2.2 Device Fingerprinting (drfdisvc.walmart.com)

Walmart runs an extremely heavy device fingerprinting system through `drfdisvc.walmart.com`:
- **Multiple requests** per page load (10+ observed)
- **Encrypted payloads** in URL parameters (hex-encoded)
- **Distinct endpoint paths** for different fingerprint stages:
  - Initial collection
  - Canvas/WebGL fingerprint
  - Audio fingerprint
  - Font enumeration
  - Browser feature detection
- **Clear pixel**: `drfdisvc.walmart.com/fp/clear.png` (1x1 tracking pixel)

### 2.3 ThreatMetrix (LexisNexis)

Two variants observed:
- `h.online-metrix.net/*` — Standard ThreatMetrix
- `h64.online-metrix.net/*` — 64-bit variant

Both carry encrypted payload parameters for device risk scoring.

### 2.4 What PerimeterX Measures

PerimeterX (`_px` cookies) evaluates:
- **Mouse movement entropy** — Real humans have varied, curved mouse paths. Bots move in straight lines or don't move at all.
- **Keyboard timing** — Uniform delays are suspicious. Real typing has variable inter-key delays (30-120ms).
- **Scroll behavior** — Real users scroll. Bots that never scroll are flagged.
- **Time on page** — Jumping through pages in < 1 second is not human.
- **Navigation pattern** — Going directly to login without visiting homepage first is suspicious.
- **Canvas/WebGL fingerprint** — Must match a real browser. Stealth JS handles this.
- **Focus/blur events** — Tab switching, window focus changes.
- **Touch vs mouse** — Consistency between claimed device and input method.
- **Collector frequency** — PX collector is called repeatedly; gaps in collection are suspicious.

---

## 3. Post-Login API Endpoints

### 3.1 Bootstrap Config

```
GET /orchestra/api/ccm/v3/bootstrap?configNames=account,ads,amends,auto_care_center,
    bookslot,brandpage,cart,category,checkout,converse,conversations,onefinance,
    onepaylater,onepaylaterinstore,fittingroom,footer,gcomm,header,help,homepage,
    identity,list,marketplace,nonprofits,nonprofits_features,orders,ordertracking,
    payments,phts_hvac,product,pulse,purchasehistory,registry,replenishment,
    repurchase,returns,reviews,search,search_shop,search_typeahead,shared,wireless,
    onedebitcard,onepaywallet,onecreditcard,wplus,storepages,subscription,contentpage,
    wallet,affil_live_streaming,wmsavings,flyer,rewards,wmcash,inhome,
    hw_visioncenter,omniScheduler,optical_nonbundled,social-share,extended_reality,
    cakes,services-sso,order_ereceipt,intl_remarketing,inStoreWifi
```

This is the primary endpoint for validating session health. If this returns 200 with
valid config data, the session is authenticated. Returns 403 if cookies are
expired/invalid.

### 3.2 Account Landing Page (GraphQL)

```
GET /orchestra/cph/graphql/accountLandingPage/<hash>?variables={...}
```

Variables (URL-encoded JSON):
```json
{
  "isCashiLinked": false,
  "enableCountryCallingCode": false,
  "enablePhoneCollection": false,
  "enableMembershipId": false,
  "enableMembershipAutoRenew": false,
  "enableMembershipQuery": true,
  "enableWcp": false,
  "enableMembershipAutoRenewalModal": false,
  "includeResidencyRegionInfo": false,
  "sessionInput": {},
  "includeSessionInfo": false
}
```

### 3.3 Notifications

```
GET /orchestra/cph/graphql/FetchNotifications/<hash>?variables={...}
```

Variables:
```json
{
  "input": {
    "pageInfo": { "size": 100 },
    "platform": "WEB"
  }
}
```

### 3.4 Cart Merge

```
POST /orchestra/cartxo/graphql/MergeAndGetCart/<hash>
```

Called after login to merge any guest cart with the authenticated cart.

### 3.5 SWAG GraphQL

```
POST /swag/graphql
```

Called twice after login. Session/wallet management queries.

### 3.6 Reviews

```
GET /orchestra/cph/graphql/multiReviews/<hash>?variables={...}
```

Variables:
```json
{
  "page": 1,
  "limit": 1,
  "pageType": "PENDING_REVIEW_COUNT",
  "enableReviewItems": false
}
```

---

## 4. Frontend Architecture

### 4.1 Next.js Application

Walmart.com is built with **Next.js** (React SSR framework):
- Build ID: `production_20260318T052534679Z-en-US`
- React version: 18.2
- Static assets served from CDN: `i5.walmartimages.com/dfw/63fd9f59-f68f/...`

### 4.2 Key Page Chunks

| Page | Chunk File |
|------|-----------|
| Orders (purchase history) | `pages/orders-5d443fda7051e8d7.js` |
| Cart | `pages/cart-50eaa854f9a4d23b.js` |
| App Shell | `pages/_app-6a0947e062fe14e2.js` |
| Write Review | `pages/reviews/write-review-e1d09da08bf78edf.js` |

### 4.3 Key UI Components (from chunk names)

- `orders_purchase-history-v2_index-page` — Order history
- `orders_order-status-tracker` — Order tracking
- `orders_details_page-context_details-provider` — Order details
- `cart_product-tile-container_cart-product-tile` — Cart items
- `checkout_bookslot-shortcut` — Delivery time slot selection
- `payments_context_context_wallet-state-context-provider` — Payment wallet
- `identity-next_one-tap-auth` — One-tap authentication
- `account_menu_menu` — Account navigation menu
- `ui_captcha` — CAPTCHA handling component

### 4.4 Design System

- **LivingDesign**: `@livingdesign/react@1.16.1` — Walmart's internal design system
- **UI Icons**: `ui-icons.0a573231.woff2` — Custom icon font
- **Fonts**: EverydaySans-Regular, EverydaySans-Bold (custom Walmart fonts)

---

## 5. Session Validation Strategy

### 5.1 How to Verify Session Health

The most reliable method to check if session cookies are valid:

```python
# Primary validation — bootstrap config endpoint
resp = await client.get(
    "https://www.walmart.com/orchestra/api/ccm/v3/bootstrap",
    params={"configNames": "account,orders,identity"},
)
if resp.status_code == 200:
    # Session is valid
elif resp.status_code == 403:
    # Session expired or cookies invalid
```

### 5.2 Required Cookies

For an authenticated session, the following cookies are critical:
- **Session cookies**: `session_token`, authentication tokens set by `/account/verifyToken`
- **PerimeterX cookies**: `_pxvid`, `_px`, `_px3`, `_pxhd`, `_pxde`
- **Device fingerprint cookies**: Set by `drfdisvc.walmart.com`
- **ThreatMetrix cookies**: Set by `online-metrix.net`

### 5.3 Session Import Approach

Since Walmart's PerimeterX + ThreatMetrix + device fingerprinting stack makes
programmatic login extremely difficult, the recommended approach is:

1. User logs in via a real browser
2. Export cookies using a browser extension (e.g., EditThisCookie, Cookie-Editor)
3. Import cookies into Pmon via Dashboard Settings > Session Cookies
4. Validate via bootstrap endpoint
5. Re-import when session expires (typically 24-48 hours)

---

## 6. Ad & Tracking Infrastructure

### 6.1 Walmart Connect (Advertising)

Walmart runs its own ad platform (Walmart Connect):
- **Ad serving**: `advertising.walmart.com/thunder/assets/...`
- **Impression tracking**: `/dad/trk/v1/im?encrypted=...` (encrypted JWE payloads)
- **Viewability tracking**: `/dad/trk/v1/vi?encrypted=...&type=viewable`
- **Ad placements observed**: skyline1, marquee1 (on order history page)

### 6.2 XL Media

Heavy viewability tracking:
- **Pagespeed**: `s.xlgmedia.com/static/2.179.0/pagespeed.js`
- **Main tracker**: `s.xlgmedia.com/static/2.179.0/main.js`
- **Config**: `s.xlgmedia.com/o/config.json`
- **Postbacks**: Multiple per page load with viewability metrics (`vw`, `cv=3`)
- **Parameters**: `ci=469244`, `pd=avt`, `pp=WMT`, `sr=walmart.com`

---

## 7. Network Request Patterns

### 7.1 Request Sequence After Login

The observed request sequence after successful login:

1. **verifyToken redirect** → `/account/verifyToken?code=...`
2. **Final redirect** → `/orders?action=SignIn&rm=true`
3. **CSS/JS bundle loading** (CDN: `i5.walmartimages.com`)
4. **PX init** → `/px/PXu6b0qd2S/init.js`
5. **Beacon** → `beacon.js`
6. **Bootstrap** → `/orchestra/api/ccm/v3/bootstrap`
7. **Account data** → `/orchestra/cph/graphql/accountLandingPage/`
8. **Notifications** → `/orchestra/cph/graphql/FetchNotifications/`
9. **Cart merge** → `/orchestra/cartxo/graphql/MergeAndGetCart/`
10. **SWAG** → `/swag/graphql` (x2)
11. **Device fingerprinting** → `drfdisvc.walmart.com/*` (10+ requests)
12. **ThreatMetrix** → `h.online-metrix.net/*`, `h64.online-metrix.net/*`
13. **PX Collector** → `collector-pxu6b0qd2s.px-cloud.net/api/v2/collector` (repeated)
14. **RUM beacons** → `b.www.walmart.com/rum.gif` (multiple)
15. **Ad viewability** → `s.xlgmedia.com/*` (extensive)
16. **PX CAPTCHA check** → `/px/PXu6b0qd2S/captcha/captcha.js`
17. **Reviews count** → `/orchestra/cph/graphql/multiReviews/`
18. **Account menu** → `account_menu_menu-*.js`, `account_data-access_helpers_gcomm-utils.*.js`
19. **Notification center** → `ui_notification-center_component.*.js`

### 7.2 CDN Structure

Static assets follow this pattern:
```
https://i5.walmartimages.com/dfw/<project-id>/<build-hash>/v2/en-US/_next/static/chunks/<chunk-name>-<hash>.js
```

Current build hash: `874c1b8b-9967-4744-b87a-ffe810fe894c`

### 7.3 GraphQL Hash Pattern

Walmart's GraphQL endpoints use persisted query hashes:
```
/orchestra/<namespace>/graphql/<OperationName>/<sha256-hash>?variables={...}
```

Namespaces observed:
- `cph` — Customer Platform Hub (account, notifications, reviews)
- `home` — Product data
- `cartxo` — Cart operations

---

## 8. Browser Automation Considerations

### 8.1 Why API Login Fails

Walmart's bot protection stack makes API-level login nearly impossible:
1. **PerimeterX** blocks requests without proper browser fingerprint
2. **ThreatMetrix** requires real device intelligence signals
3. **drfdisvc** device fingerprinting needs active JavaScript execution
4. **CAPTCHA** ("press & hold") triggers on suspected bot traffic
5. **Partytown** worker isolation makes script interception difficult

### 8.2 Browser-Based Login Strategy

For Playwright-based login:

1. **Warm-up**: Visit walmart.com homepage first (let PX/ThreatMetrix initialize)
2. **Navigate to login**: `/account/login`
3. **Wait for PX**: Ensure `client.px-cloud.net` scripts load and collector fires
4. **Enter credentials**: With human-like typing delays (variable 30-120ms)
5. **Mouse movement**: Natural curves to form fields and submit button
6. **Wait for verifyToken**: Monitor for `/account/verifyToken?code=` redirect
7. **Extract cookies**: After redirect completes, capture all cookies
8. **Validate**: Call bootstrap endpoint to confirm session

### 8.3 CAPTCHA Handling

If "press & hold" CAPTCHA appears:
- The CAPTCHA script loads from `/px/PXu6b0qd2S/captcha/captcha.js`
- UI component: `ui_captcha` chunk
- **Cannot be solved programmatically** — requires human interaction
- Strategy: Save screenshot, notify user, wait for manual resolution

### 8.4 Session Persistence

After successful login, extract and store:
1. All cookies from `walmart.com` domain
2. All cookies from `.walmart.com` domain
3. PerimeterX cookies (`_px*`)
4. Any `Set-Cookie` headers from API responses

Store in `retailer_sessions` table for reuse across monitor restarts.

---

## 9. Differences from Target

| Aspect | Walmart | Target |
|--------|---------|--------|
| **OAuth client_id** | `5f3fb121-076a-45f6-9587-249f0bc160ff` | `ecom-web-1.0.0` |
| **Auth code format** | 32-char uppercase hex | UUID |
| **Token exchange** | Server-side (`/account/verifyToken`) | Client-side JS (`/gsp/oauth_validations/v3/token_validations`) |
| **Token calls** | Single redirect (server-side) | Two client-side POST calls |
| **Bot detection** | PX + ThreatMetrix + drfdisvc (3 layers) | PX + SSX + FullStory (3 layers) |
| **CAPTCHA type** | "Press & Hold" (PX) | "Press & Hold" (PX) |
| **Framework** | Next.js (React SSR) | Custom React SPA |
| **Session validation** | `/orchestra/api/ccm/v3/bootstrap` | Various API calls |
| **Difficulty** | **Very Hard** — heaviest fingerprinting | **Hard** — complex but fewer fingerprint layers |
