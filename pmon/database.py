"""SQLite database for multi-user storage."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("PMON_DB_PATH", Path(__file__).parent.parent / "data" / "pmon.db"))

_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _init_tables(_conn)
    return _conn


def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            totp_secret TEXT,
            totp_enabled INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            last_login TEXT
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            name TEXT DEFAULT '',
            retailer TEXT DEFAULT '',
            quantity INTEGER DEFAULT 1,
            auto_checkout INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, url)
        );

        CREATE TABLE IF NOT EXISTS retailer_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            retailer TEXT NOT NULL,
            email TEXT DEFAULT '',
            password TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, retailer)
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            poll_interval INTEGER DEFAULT 30,
            discord_webhook TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS checkout_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT,
            retailer TEXT,
            product_name TEXT,
            status TEXT,
            order_number TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            level TEXT DEFAULT 'ERROR',
            source TEXT DEFAULT '',
            message TEXT,
            details TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


# --- User operations ---

def create_user(username: str, password_hash: str) -> int:
    db = get_db()
    cursor = db.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (username, password_hash),
    )
    user_id = cursor.lastrowid
    db.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
    db.commit()
    return user_id


def get_user(username: str) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def update_user_totp(user_id: int, secret: str, enabled: bool):
    db = get_db()
    db.execute(
        "UPDATE users SET totp_secret = ?, totp_enabled = ? WHERE id = ?",
        (secret, int(enabled), user_id),
    )
    db.commit()


def update_last_login(user_id: int):
    db = get_db()
    db.execute(
        "UPDATE users SET last_login = datetime('now') WHERE id = ?",
        (user_id,),
    )
    db.commit()


def get_user_count() -> int:
    db = get_db()
    row = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    return row["cnt"]


# --- Product operations ---

def get_user_products(user_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM products WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_product(user_id: int, url: str, name: str, retailer: str,
                quantity: int = 1, auto_checkout: bool = False) -> int:
    db = get_db()
    cursor = db.execute(
        """INSERT INTO products (user_id, url, name, retailer, quantity, auto_checkout)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, url) DO UPDATE SET
             name=excluded.name, quantity=excluded.quantity, auto_checkout=excluded.auto_checkout""",
        (user_id, url, name, retailer, quantity, int(auto_checkout)),
    )
    db.commit()
    return cursor.lastrowid


def remove_product(user_id: int, url: str):
    db = get_db()
    db.execute("DELETE FROM products WHERE user_id = ? AND url = ?", (user_id, url))
    db.commit()


def toggle_product_auto(user_id: int, url: str) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT auto_checkout FROM products WHERE user_id = ? AND url = ?",
        (user_id, url),
    ).fetchone()
    if not row:
        return False
    new_val = 0 if row["auto_checkout"] else 1
    db.execute(
        "UPDATE products SET auto_checkout = ? WHERE user_id = ? AND url = ?",
        (new_val, user_id, url),
    )
    db.commit()
    return bool(new_val)


def update_product_quantity(user_id: int, url: str, quantity: int):
    db = get_db()
    db.execute(
        "UPDATE products SET quantity = ? WHERE user_id = ? AND url = ?",
        (quantity, user_id, url),
    )
    db.commit()


# --- Retailer account operations ---

def get_retailer_accounts(user_id: int) -> dict[str, dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM retailer_accounts WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return {r["retailer"]: dict(r) for r in rows}


def set_retailer_account(user_id: int, retailer: str, email: str, password: str):
    db = get_db()
    db.execute(
        """INSERT INTO retailer_accounts (user_id, retailer, email, password)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, retailer) DO UPDATE SET email=excluded.email, password=excluded.password""",
        (user_id, retailer, email, password),
    )
    db.commit()


# --- Settings operations ---

def get_user_settings(user_id: int) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        return dict(row)
    return {"user_id": user_id, "poll_interval": 30, "discord_webhook": ""}


def update_user_settings(user_id: int, poll_interval: int | None = None,
                         discord_webhook: str | None = None):
    db = get_db()
    if poll_interval is not None:
        db.execute("UPDATE user_settings SET poll_interval = ? WHERE user_id = ?",
                   (poll_interval, user_id))
    if discord_webhook is not None:
        db.execute("UPDATE user_settings SET discord_webhook = ? WHERE user_id = ?",
                   (discord_webhook, user_id))
    db.commit()


# --- Checkout log operations ---

def add_checkout_log(user_id: int, url: str, retailer: str, product_name: str,
                     status: str, order_number: str = "", error_message: str = ""):
    db = get_db()
    db.execute(
        """INSERT INTO checkout_log (user_id, url, retailer, product_name, status, order_number, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, url, retailer, product_name, status, order_number, error_message),
    )
    db.commit()


def get_checkout_log(user_id: int, limit: int = 50) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM checkout_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Error log operations ---

def add_error_log(user_id: int | None, level: str, source: str,
                  message: str, details: str = ""):
    db = get_db()
    db.execute(
        """INSERT INTO error_log (user_id, level, source, message, details)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, level, source, message, details),
    )
    db.commit()


def get_error_log(user_id: int | None = None, limit: int = 100) -> list[dict]:
    db = get_db()
    if user_id:
        rows = db.execute(
            "SELECT * FROM error_log WHERE user_id = ? OR user_id IS NULL ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM error_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
