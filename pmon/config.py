"""Configuration management for Pmon.

AUDIT FINDINGS (2026-03-17):
=============================================================================
Data model gaps that affect the Target checkout rewrite:

1. NO max_price FIELD: Neither Product nor Profile has a max_price field.
   The checkout flow needs this to implement a price guard (abort if total
   exceeds max_price before clicking "Place order").

2. NO card_cvv IN Profile: CVV is stored in AccountCredentials (via the
   database's retailer_accounts table), not in Profile. This is correct —
   CVV should be per-retailer-account, not per-profile.

3. NO store_id FIELD: Target's API needs a store ID for pickup availability.
   Currently hardcoded to "3991" in the monitor. Should be configurable.

4. Product.auto_checkout IS BOOLEAN: No quantity support at product level
   for auto-checkout (the DB has a quantity column but it's not used in
   the checkout flow).

5. ENVIRONMENT VARIABLE OVERRIDES: Poll interval and discord webhook can
   be overridden via env vars (PMON_POLL_INTERVAL, PMON_DISCORD_WEBHOOK).
   This is good for Railway deployment.
=============================================================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_PATH = CONFIG_DIR / "config.yaml"


@dataclass
class Product:
    url: str
    name: str
    auto_checkout: bool = False
    retailer: str = ""

    def __post_init__(self):
        if not self.retailer:
            self.retailer = detect_retailer(self.url)


@dataclass
class Profile:
    name: str = "default"
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""


@dataclass
class AccountCredentials:
    email: str = ""
    password: str = ""
    card_cvv: str = ""
    card_number: str = ""
    card_exp_month: str = ""
    card_exp_year: str = ""
    card_name: str = ""
    phone_last4: str = ""
    account_last_name: str = ""


@dataclass
class Config:
    poll_interval: int = 30
    discord_webhook: str = ""
    console_notifications: bool = True
    products: list[Product] = field(default_factory=list)
    profiles: dict[str, Profile] = field(default_factory=dict)
    accounts: dict[str, AccountCredentials] = field(default_factory=dict)
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8888
    headless: bool = True
    chrome_profile_dir: str = ""


def detect_retailer(url: str) -> str:
    url_lower = url.lower()
    if "pokemoncenter.com" in url_lower:
        return "pokemoncenter"
    if "target.com" in url_lower:
        return "target"
    if "bestbuy.com" in url_lower:
        return "bestbuy"
    if "walmart.com" in url_lower:
        return "walmart"
    if "costco.com" in url_lower:
        return "costco"
    if "samsclub.com" in url_lower:
        return "samsclub"
    return "unknown"


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH

    raw = {}
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

    products = []
    for p in raw.get("products", []):
        products.append(Product(
            url=p["url"],
            name=p.get("name", ""),
            auto_checkout=p.get("auto_checkout", False),
        ))

    # Support adding products via PMON_PRODUCTS env var (comma-separated URLs)
    env_products = os.environ.get("PMON_PRODUCTS", "")
    if env_products:
        for url in env_products.split(","):
            url = url.strip()
            if url:
                products.append(Product(url=url, name="", auto_checkout=False))

    profiles = {}
    for name, pdata in raw.get("profiles", {}).items():
        profiles[name] = Profile(name=name, **{k: v for k, v in pdata.items() if v})

    accounts = {}
    for retailer, adata in raw.get("accounts", {}).items():
        if adata and (adata.get("email") or adata.get("password")):
            accounts[retailer] = AccountCredentials(**adata)

    notif = raw.get("notifications", {})
    dash = raw.get("dashboard", {})

    # Browser mode: default to headless=True for servers, but allow override
    # PMON_HEADLESS=0 or headless: false in config to run visible Chrome
    env_headless = os.environ.get("PMON_HEADLESS")
    if env_headless is not None:
        headless = env_headless.lower() not in ("0", "false", "no")
    else:
        headless = raw.get("headless", True)

    chrome_profile_dir = os.environ.get(
        "PMON_CHROME_PROFILE",
        raw.get("chrome_profile_dir", ""),
    )

    return Config(
        poll_interval=int(os.environ.get("PMON_POLL_INTERVAL", raw.get("poll_interval", 30))),
        discord_webhook=os.environ.get("PMON_DISCORD_WEBHOOK", notif.get("discord_webhook", "")),
        console_notifications=notif.get("console", True),
        products=products,
        profiles=profiles,
        accounts=accounts,
        dashboard_host=dash.get("host", "127.0.0.1"),
        dashboard_port=dash.get("port", 8888),
        headless=headless,
        chrome_profile_dir=chrome_profile_dir,
    )


def save_config(config: Config, path: Path | None = None):
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "poll_interval": config.poll_interval,
        "notifications": {
            "discord_webhook": config.discord_webhook,
            "console": config.console_notifications,
        },
        "products": [
            {"url": p.url, "name": p.name, "auto_checkout": p.auto_checkout}
            for p in config.products
        ],
        "profiles": {
            name: {
                "first_name": p.first_name,
                "last_name": p.last_name,
                "email": p.email,
                "phone": p.phone,
                "address_line1": p.address_line1,
                "address_line2": p.address_line2,
                "city": p.city,
                "state": p.state,
                "zip_code": p.zip_code,
            }
            for name, p in config.profiles.items()
        },
        "accounts": {
            name: {"email": a.email, "password": a.password}
            for name, a in config.accounts.items()
        },
        "headless": config.headless,
        "chrome_profile_dir": config.chrome_profile_dir,
        "dashboard": {
            "host": config.dashboard_host,
            "port": config.dashboard_port,
        },
    }

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
