"""Selector registry for all retailers.

REVIEWED [Mission 1] — Centralized selector management.
All CSS selectors, XPaths, and text matchers must be referenced through
this registry. No selector string should appear inline in automation logic.
"""

from .target import TARGET_SELECTORS
from .walmart import WALMART_SELECTORS
from .pokemoncenter import POKEMONCENTER_SELECTORS

SELECTORS = {
    "target": TARGET_SELECTORS,
    "walmart": WALMART_SELECTORS,
    "pokemoncenter": POKEMONCENTER_SELECTORS,
}


def get_selectors(retailer: str) -> dict:
    """Get the selector registry for a retailer."""
    return SELECTORS.get(retailer, {})
