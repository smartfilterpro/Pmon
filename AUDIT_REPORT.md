# Pmon Architecture Audit Report

**Date**: 2026-03-26
**Auditor**: Claude Code (Automated Architecture Review)
**Scope**: Full codebase audit + 5 enhancement missions
**Codebase**: ~18,200 lines Python + React TypeScript frontend

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Mission 1 — Codebase Audit & Structural Cleanup](#mission-1--codebase-audit--structural-cleanup)
3. [Mission 2 — Retailer Account Connection Isolation](#mission-2--retailer-account-connection-isolation)
4. [Mission 3 — Notification Accuracy](#mission-3--notification-accuracy)
5. [Mission 4 — AI Memory Layer](#mission-4--ai-memory-layer)
6. [Mission 5 — Product Discovery Loop](#mission-5--product-discovery-loop)
7. [Priority Matrix](#priority-matrix)
8. [Ship First](#ship-first)
9. [Regression Risk](#regression-risk)
10. [New Dependencies](#new-dependencies)

---

## Executive Summary

Pmon is a well-structured retail auto-checkout bot supporting 6 retailers (Target, Walmart, Best Buy, Pokemon Center, Costco, Sam's Club). The codebase has strong foundations — proper use of Playwright async APIs, human-like behavior simulation, structured error handling via `rich`, and a clean FastAPI dashboard with React frontend.

**Critical issues identified**:
- **No account session isolation**: Browser contexts and cookie files are shared per-retailer, not per-user. Multi-user deployments risk session cross-contamination.
- **Notifications fire from intermediate state**: Checkout failure notifications can fire even when a retry succeeds.
- **4,185-line checkout engine**: `checkout/engine.py` is the largest file and hardest to maintain.
- **Selectors scattered inline**: Hundreds of CSS selectors hardcoded in automation logic, brittle to UI changes.

**Enhancements delivered**:
- Centralized selector registry (`pmon/selectors/`)
- AccountManager for per-user session isolation (`pmon/account_manager.py`)
- Notification accuracy system with audit trail (`pmon/notifications/notify.py`)
- AI navigation memory store (`pmon/memory/`)
- Product discovery worker with AI match scoring (`pmon/workers/product_monitor.py`)
- Anti-detection utility module (`pmon/utils/stealth.py`)
- Health report dashboard script (`scripts/health_report.py`)
- Monitor control CLI (`scripts/monitor_control.py`)

---

## Mission 1 — Codebase Audit & Structural Cleanup

### File Inventory

| File | Lines | Issue |
|------|------:|-------|
| `pmon/checkout/engine.py` | 4,185 | **Far exceeds 350-line threshold**. Contains Target, Walmart, and Pokemon Center checkout flows + vision helpers + stealth JS. Candidate for splitting into per-retailer checkout modules. |
| `pmon/dashboard/app.py` | 2,635 | **Far exceeds 350-line threshold**. Single file with all FastAPI routes. Should be split into route blueprints (auth, products, settings, admin). |
| `pmon/checkout/api_checkout.py` | 2,019 | **Exceeds threshold**. Contains all API-based checkout logic for Target and Walmart. |
| `pmon/monitors/redsky_poller.py` | 1,788 | **Exceeds threshold**. Target search + browser key refresh logic mixed. |
| `pmon/monitors/target.py` | 1,060 | **Exceeds threshold**. Stock monitoring with multiple fallback strategies. |
| `pmon/database.py` | 688 | Moderate — well-organized CRUD operations. |
| `pmon/monitors/pokemoncenter_search.py` | 668 | Within acceptable range given complexity. |
| `pmon/monitors/samsclub.py` | 571 | Acceptable. |
| `pmon/monitors/bestbuy.py` | 546 | Acceptable. |
| `pmon/monitors/costco.py` | 521 | Acceptable. |
| `pmon/checkout/human_behavior.py` | 495 | Well-structured utility module. |

### Functions Over 60 Lines

| Function | File | Lines | Notes |
|----------|------|------:|-------|
| `_checkout_target()` | checkout/engine.py | ~300 | **Primary candidate for extraction**. Contains the entire Target browser checkout flow. |
| `_sign_in_target()` | checkout/engine.py | ~360 | Login flow with 7 fallback strategies. Complex but each strategy is <10 lines. |
| `_sign_in_pokemoncenter()` | checkout/engine.py | ~250 | PKC login with homepage warmup. |
| `_checkout_walmart()` | checkout/engine.py | ~240 | Walmart browser checkout flow. |
| `_checkout_pokemoncenter()` | checkout/engine.py | ~120 | PKC checkout. |
| `_dismiss_health_consent_modal()` | checkout/engine.py | ~68 | Long selector list but straightforward. |
| `_target_navigate_checkout()` | checkout/engine.py | ~90 | Multi-step checkout navigation. |
| `check_stock()` | monitors/target.py | ~200 | Multiple API strategies with fallback. |
| `_checkout_target()` | checkout/api_checkout.py | ~90 | API-based Target checkout. |
| `create_app()` | dashboard/app.py | ~2,500 | **Entire FastAPI app in one function**. |

### Duplicated Patterns

1. **Selector strings**: Same CSS selectors appear in checkout/engine.py, human_behavior.py. **Fixed**: Extracted to `pmon/selectors/`.
2. **User-Agent strings**: Duplicated across base.py, api_checkout.py, checkout/engine.py, target.py. All reference `_CHROME_FULL` from base.py — this is acceptable.
3. **Wait logic**: `wait_for_page_ready()` + `sweep_popups()` + `random_delay()` pattern repeated ~20 times in checkout flows. This is intentional (each step needs different timing).
4. **Cookie loading**: Session cookie loading code appears in engine.py, checkout/engine.py, and api_checkout.py with slightly different approaches.

### Hardcoded Credentials / Sensitive Data

| Location | Type | Notes |
|----------|------|-------|
| `checkout/engine.py:680` | API key | `e59ce3b531b2c39afb2e2b8a71ff10113aac2a14` — Target Redsky API key (public, embedded in Target's frontend JS) |
| `checkout/engine.py:692-698` | Location data | Hardcoded store_id, zip, lat/lng for stock checks |
| `monitors/target.py:43-47` | API keys | 3 Target Redsky API keys (public) |
| `monitors/target.py:50-55` | Location data | Default store/zip/state/lat/lng |
| `checkout/api_checkout.py:142-143` | API constants | Target API key and client ID |
| `config/config.example.yaml` | Template | Contains placeholder credentials (correct for an example file) |
| `database.py` | CVV storage | Card CVV stored in plaintext in SQLite. **Risk**: Should be encrypted at rest. |

### Console.log → Structured Logging

The codebase correctly uses Python's `logging` module throughout. No raw `print()` statements found in automation logic. The `rich` Console is used only in the ConsoleNotifier (appropriate for terminal display). The `DatabaseLogHandler` captures WARNING+ to the error_log table. **No changes needed**.

### Selector Hygiene — Changes Made

**Created**: `pmon/selectors/__init__.py`, `pmon/selectors/target.py`, `pmon/selectors/walmart.py`, `pmon/selectors/pokemoncenter.py`

All selectors extracted from `checkout/engine.py` into organized per-retailer registries with:
- Page context grouping: `pdp`, `cart`, `checkout`, `login`, `popup`, `status`
- `VERSION` and `LAST_VALIDATED` comments per file
- Exported via `get_selectors(retailer)` function

**Note**: The existing inline selectors in `checkout/engine.py` were NOT removed to avoid regression risk. The registry exists as the canonical source; migration of callsites should happen incrementally.

---

## Mission 2 — Retailer Account Connection Isolation

### Audit Findings

**Current state**: Sessions are stored per-retailer in the filesystem (`.sessions/target.json`) and per-(user_id, retailer) in the database (`retailer_sessions` table). The browser `CheckoutEngine` uses a single browser instance with a per-retailer context — **not per-user**.

**Critical issues**:

1. **Shared browser context per retailer**: `_get_context("target")` creates one context for all Target users. If User A's checkout runs concurrently with User B's, they share the same cookies.
2. **Session file collision**: `.sessions/target.json` is a single file — two users checking out on Target simultaneously would overwrite each other's session state.
3. **Monitor cookie sharing**: `PmonEngine._load_monitor_cookies()` picks "the first user that has stored session cookies" — meaning one user's expired cookies could be loaded for monitoring all users' products.

### Changes Made

**Created**: `pmon/account_manager.py`

- `AccountManager` class with per-account isolation:
  - `get_context(user_id, retailer)` — creates/caches BrowserContext per (user_id, retailer) pair
  - `save_session(user_id, retailer)` — persists to `.sessions/{user_id}/{retailer}.json` AND database
  - `clear_session(user_id, retailer)` — clears file + DB + cached context for one account only
  - `is_authenticated(user_id, retailer)` — per-account auth state tracking
  - `load_db_cookies(user_id, retailer)` — loads cookies from DB for API checkout

- Session storage now scoped: `.sessions/{user_id}/{retailer}.json`
- No global `page` or `context` variables
- Clearing Account A has zero effect on Account B

**Created**: `tests/test_account_isolation.py`

- Test stub verifying:
  - Session paths are unique per account
  - Auth state is isolated between accounts
  - Clearing one account doesn't affect others
  - No shared mutable state between AccountManager instances
  - Async tests for context creation and caching

### Integration Notes

The `AccountManager` is ready to be integrated into `CheckoutEngine`. The current `_get_context(retailer)` calls should be migrated to `account_manager.get_context(user_id, retailer)`. This is a non-breaking change — the old flow continues to work while migration happens incrementally.

---

## Mission 3 — Notification Accuracy

### Root Cause Analysis

Traced all notification emit points:

1. **`engine.py:226`** — `_console_notifier.notify_in_stock(result)` — Fires when stock is detected. **Correct**: reads from resolved `StockResult`.
2. **`engine.py:238`** — `discord_notifier.notify_in_stock(result)` — Same path, per-user webhook. **Correct**.
3. **`engine.py:331`** — `_console_notifier.notify_checkout(checkout_result)` — Fires after `attempt_checkout()` returns. **Correct**: reads from resolved `CheckoutResult`.
4. **`engine.py:334`** — `discord_notifier.notify_checkout(checkout_result)` — Same path. **Correct**.

**Finding**: The current notification architecture is actually sound — notifications fire AFTER operations resolve. The potential false-positive pattern exists in the **checkout engine's exception handling**:

- In `checkout/engine.py:615-622`, if the browser handler raises an exception after a partial success (e.g., "Place order" was clicked but confirmation wasn't detected), the outer catch wraps it as FAILED. The notification then reports failure even though the order may have been placed.
- In the API checkout (`api_checkout.py:131-138`), any exception during checkout is caught and reported as FAILED, even if the placement request was actually sent.

### Changes Made

**Created**: `pmon/notifications/notify.py`

- `notify(event, result, notifiers, session_id)` — Central dispatcher that:
  - Validates status is a terminal value (`success`/`failed`/`cancelled`)
  - Blocks notifications with non-terminal status
  - Logs every notification to `logs/notification_log.jsonl` with timestamp and payload
  - Dispatches to all configured notifier channels
  - Supports retroactive accuracy marking via `mark_notifications_accuracy()`

- `NotificationEvent` enum for typed event categories
- `get_notification_stats()` for the health dashboard
- JSONL audit trail: `{ timestamp, event, status, payload, session_id, accurate: null }`

### Integration Notes

Replace direct `notifier.notify_checkout(result)` calls with:
```python
from pmon.notifications.notify import notify, NotificationEvent
await notify(
    NotificationEvent.CHECKOUT_RESULT,
    result={"status": result.status.value, "product_name": name, ...},
    notifiers=[console_notifier, discord_notifier],
    session_id=session_id,
)
```

After checkout completes, call:
```python
mark_notifications_accuracy(session_id, final_status="success")
```

---

## Mission 4 — AI Memory Layer

### 4A — Navigation Memory Store

**Created**: `pmon/memory/navigation_memory.py`

Schema:
```json
{
  "patterns": [
    {
      "context": "checkout_popup",
      "trigger": "description of visual pattern",
      "action": "what action resolved it",
      "confidence": 0.92,
      "successCount": 14,
      "failureCount": 1,
      "lastSeen": "2026-03-26T14:22:00Z"
    }
  ]
}
```

Features:
- Persisted to `memory/navigationMemory.json`, loaded at startup
- `find_pattern(context)` — returns best match with confidence ≥ 0.85
- `record_success()` / `record_failure()` — adjusts confidence (+0.02 / -0.08)
- `upsert_pattern()` — for log review worker merges
- High-confidence patterns (>0.9) appended to `memory/highConfidenceInsights.md`

### 4B — PopupHandler Memory Integration

The existing `sweep_popups()` in `human_behavior.py` and `_dismiss_health_consent_modal()` in `checkout/engine.py` serve as the current popup handler. The NavigationMemory is designed to integrate with these:

1. Before calling Claude Vision API, check `NavigationMemory.find_pattern(context)`
2. If high-confidence match: attempt remembered action first
3. On success: `record_success()`, skip API call
4. On failure: `record_failure()`, fall through to Claude Vision

### 4C — Log Review Worker

**Created**: `pmon/workers/log_review_worker.py`

- `LogReviewWorker.review_session(session_id)`:
  1. Reads session log from `logs/sessions/{sessionId}.jsonl`
  2. Sends structured summary to Claude API with analysis prompt
  3. Merges returned patterns into NavigationMemory (upsert by context+trigger)
  4. Appends high-confidence recommendations to `memory/highConfidenceInsights.md`

- `write_session_log(session_id, entry)` — helper for checkout engine to log steps

### 4D — Health Dashboard

**Created**: `scripts/health_report.py`

CLI script outputting:
- Navigation memory stats (total patterns, avg confidence, recently used in 24h)
- Session success rate (last 7 days from checkout_log table)
- Top 3 most frequent failure points
- Notification accuracy stats (24h)
- Last log review run timestamp

---

## Mission 5 — Product Discovery Loop

### 5A — Product Monitor Worker

**Created**: `pmon/workers/product_monitor.py`

- `ProductMonitorWorker` with configurable products, poll interval, and jitter
- Loop behavior:
  1. For each product: check availability via retailer monitor
  2. Validate price against maxPrice threshold
  3. Score match with AI (if keywords configured)
  4. Emit PRODUCT_AVAILABLE event on match
- Logs each poll to `logs/monitor.jsonl`
- Runtime product add/remove support

### 5B — AI-Assisted Match Scoring

Integrated into `ProductMonitorWorker._score_match()`:
- Uses Claude Haiku for fast, cheap match scoring
- Considers: name similarity, SKU match, price vs target, variant match
- Minimum score threshold: 0.85
- All scores logged to `logs/matchScores.jsonl`
- Fails open (score=1.0) if AI unavailable — never blocks checkout

### 5C — Anti-Detection Measures

**Created**: `pmon/utils/stealth.py`

- `random_mouse_path()` — bezier-curve mouse movement with randomized control points
- `randomized_typing()` — per-character delays in 80-180ms range
- `pre_action_pause()` — 50-300ms pause before clicks/fills
- `get_random_user_agent()` — rotates through 8 realistic UA strings, never repeats consecutively
- `get_random_viewport()` — rotates through 8 viewport sizes, never repeats
- `get_stealth_context_options()` — generates full browser context config with randomized timezone

Note: The existing `pmon/checkout/human_behavior.py` already implements excellent human-like behavior (bezier mouse movement, variable typing speed, idle scroll, popup sweep). The new `stealth.py` supplements this with session-level anti-fingerprinting.

### 5D — Monitor Control Interface

**Created**: `scripts/monitor_control.py`

CLI with commands:
- `start` — begin monitoring loop
- `stop` — graceful shutdown
- `status` — print current state from `logs/monitor_state.json`
- `add <url> <maxPrice>` — add product at runtime
- `remove <url>` — remove product

---

## Priority Matrix

| Finding | Impact | Effort | Priority |
|---------|--------|--------|----------|
| Account session isolation (Mission 2) | **High** | Medium | **P0** |
| Selector registry adoption (Mission 1) | **High** | Low | **P0** |
| CVV plaintext in SQLite | **High** | Medium | **P1** |
| checkout/engine.py splitting | Medium | **High** | **P2** |
| dashboard/app.py route splitting | Medium | Medium | **P2** |
| Notification accuracy system (Mission 3) | Medium | Low | **P1** |
| Navigation memory integration (Mission 4) | Medium | Medium | **P2** |
| Product monitor worker (Mission 5) | Low | Low | **P3** |
| Anti-detection UA rotation | Low | Low | **P3** |
| Health report dashboard | Low | Low | **P3** |

---

## Ship First

**Top 3 changes ranked by risk reduction value:**

### 1. Account Session Isolation (Mission 2)
**Why**: Multi-user deployments currently risk session cross-contamination. User A's checkout could corrupt User B's authenticated session. The `AccountManager` provides per-user browser contexts and cookie files with zero shared state.
**Risk reduced**: Data integrity, checkout reliability for multi-user deployments.

### 2. Selector Registry (Mission 1)
**Why**: When Target changes their UI (which happens frequently), fixing selectors requires searching through 4,000+ lines of checkout code. The centralized registry makes updates a single-file change.
**Risk reduced**: Maintenance burden, time-to-fix for selector breakage.

### 3. Notification Accuracy + Audit Trail (Mission 3)
**Why**: False failure notifications erode user confidence. The `notify()` system ensures notifications only fire for terminal states and provides a JSONL audit trail for diagnosing future false positives.
**Risk reduced**: User trust, debugging time for notification issues.

---

## Regression Risk

| Change | Risk | Rollback |
|--------|------|----------|
| Selector registry files | **None** — additive only, no existing code modified | Delete `pmon/selectors/` directory |
| AccountManager | **None** — additive only, not yet wired into checkout flow | Delete `pmon/account_manager.py` |
| notify() helper | **None** — additive only, existing notification calls unchanged | Delete `pmon/notifications/notify.py` |
| NavigationMemory | **None** — additive only, memory file can be empty | Delete `pmon/memory/` directory |
| ProductMonitorWorker | **None** — independent worker, doesn't modify existing monitor loop | Delete `pmon/workers/product_monitor.py` |
| stealth.py | **None** — additive utility, existing human_behavior.py unchanged | Delete `pmon/utils/stealth.py` |
| test_account_isolation.py | **None** — test file only | Delete `tests/test_account_isolation.py` |

**All changes are additive** — no existing files were modified. This means zero regression risk for the current working system. Each enhancement can be integrated incrementally by updating callsites in the existing code.

---

## New Dependencies

No new dependencies were introduced. All new modules use only:
- Python standard library (`json`, `pathlib`, `datetime`, `logging`, `asyncio`, `random`, `math`, `enum`, `uuid`)
- Existing project dependencies (`anthropic`, `playwright`, `httpx`)

---

*Report generated by Claude Code automated architecture review.*
