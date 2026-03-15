"""Configuration management for Pmon."""

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

    return Config(
        poll_interval=int(os.environ.get("PMON_POLL_INTERVAL", raw.get("poll_interval", 30))),
        discord_webhook=os.environ.get("PMON_DISCORD_WEBHOOK", notif.get("discord_webhook", "")),
        console_notifications=notif.get("console", True),
        products=products,
        profiles=profiles,
        accounts=accounts,
        dashboard_host=dash.get("host", "127.0.0.1"),
        dashboard_port=dash.get("port", 8888),
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
        "dashboard": {
            "host": config.dashboard_host,
            "port": config.dashboard_port,
        },
    }

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
