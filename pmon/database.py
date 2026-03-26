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

def _default_db_path() -> Path:
    """Pick a persistent DB path.

    On Railway, the ephemeral filesystem is wiped every deploy.  If the user
    attaches a volume (recommended mount: /data), we store the DB there so it
    survives redeploys.  Falls back to the project-local ./data/ directory for
    local development.
    """
    explicit = os.environ.get("PMON_DB_PATH")
    if explicit:
        return Path(explicit)

    # Railway volume mount — conventional path
    railway_volume = Path("/data")
    if os.environ.get("RAILWAY_ENVIRONMENT") and railway_volume.is_dir():
        return railway_volume / "pmon.db"

    # Local development fallback
    return Path(__file__).parent.parent / "data" / "pmon.db"


DB_PATH = _default_db_path()

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
            is_admin INTEGER DEFAULT 0,
            approved INTEGER DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS retailer_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            retailer TEXT NOT NULL,
            cookies_json TEXT DEFAULT '{}',
            headers_json TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, retailer)
        );

        CREATE TABLE IF NOT EXISTS otp_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            retailer TEXT NOT NULL,
            context TEXT DEFAULT '',
            code TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)
    conn.commit()

    # Migrate: add columns if missing (for existing databases)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """Add columns that may not exist in older databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    if "approved" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN approved INTEGER DEFAULT 0")

    # Add api_key and spend_limit to user_settings
    settings_cols = {row[1] for row in conn.execute("PRAGMA table_info(user_settings)").fetchall()}
    if "api_key" not in settings_cols:
        conn.execute("ALTER TABLE user_settings ADD COLUMN api_key TEXT DEFAULT ''")
    if "spend_limit" not in settings_cols:
        conn.execute("ALTER TABLE user_settings ADD COLUMN spend_limit REAL DEFAULT 0")

    # Add card fields to retailer_accounts
    # REVIEWED [Mission 4A] — CVV MUST NEVER BE STORED per PCI-DSS requirements.
    # The card_cvv column is kept for backward compat but wiped on migration.
    # CVV is now accepted as a transient runtime parameter only.
    acct_cols = {row[1] for row in conn.execute("PRAGMA table_info(retailer_accounts)").fetchall()}
    if "card_cvv" not in acct_cols:
        conn.execute("ALTER TABLE retailer_accounts ADD COLUMN card_cvv TEXT DEFAULT ''")
    if "card_number" not in acct_cols:
        conn.execute("ALTER TABLE retailer_accounts ADD COLUMN card_number TEXT DEFAULT ''")
    if "card_exp_month" not in acct_cols:
        conn.execute("ALTER TABLE retailer_accounts ADD COLUMN card_exp_month TEXT DEFAULT ''")
    if "card_exp_year" not in acct_cols:
        conn.execute("ALTER TABLE retailer_accounts ADD COLUMN card_exp_year TEXT DEFAULT ''")
    if "card_name" not in acct_cols:
        conn.execute("ALTER TABLE retailer_accounts ADD COLUMN card_name TEXT DEFAULT ''")
    if "phone_last4" not in acct_cols:
        conn.execute("ALTER TABLE retailer_accounts ADD COLUMN phone_last4 TEXT DEFAULT ''")
    if "account_last_name" not in acct_cols:
        conn.execute("ALTER TABLE retailer_accounts ADD COLUMN account_last_name TEXT DEFAULT ''")

    # Add price_amount to checkout_log for spend tracking
    checkout_cols = {row[1] for row in conn.execute("PRAGMA table_info(checkout_log)").fetchall()}
    if "price_amount" not in checkout_cols:
        conn.execute("ALTER TABLE checkout_log ADD COLUMN price_amount REAL DEFAULT 0")

    # Add last_in_stock_at to products for tracking when a product was last seen in stock
    product_cols = {row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "last_in_stock_at" not in product_cols:
        conn.execute("ALTER TABLE products ADD COLUMN last_in_stock_at TEXT")

    # REVIEWED [Mission 4A] — Wipe any stored CVV values (PCI-DSS compliance).
    # CVV must never be persisted; it is now accepted as a runtime-only parameter.
    try:
        wiped = conn.execute(
            "UPDATE retailer_accounts SET card_cvv = '' WHERE card_cvv != ''"
        ).rowcount
        if wiped:
            logger.warning(
                "PCI-DSS migration: wiped %d stored CVV value(s). "
                "Users must re-enter CVV at checkout time.", wiped
            )
    except Exception:
        pass  # Column may not exist yet on brand new databases

    conn.commit()


# --- User operations ---

def create_user(username: str, password_hash: str,
                is_admin: bool = False, approved: bool = False) -> int:
    db = get_db()
    cursor = db.execute(
        "INSERT INTO users (username, password_hash, is_admin, approved) VALUES (?, ?, ?, ?)",
        (username, password_hash, int(is_admin), int(approved)),
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


def approve_user(user_id: int):
    db = get_db()
    db.execute("UPDATE users SET approved = 1 WHERE id = ?", (user_id,))
    db.commit()


def reject_user(user_id: int):
    """Delete a pending user."""
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ? AND approved = 0", (user_id,))
    db.commit()


def get_pending_users() -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, username, created_at FROM users WHERE approved = 0 ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_users() -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, username, is_admin, approved, created_at, last_login FROM users ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def set_user_admin(user_id: int, is_admin: bool):
    db = get_db()
    db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
    db.commit()


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


def update_last_in_stock(url: str):
    """Update last_in_stock_at for all rows matching *url* (across all users)."""
    conn = get_db()
    conn.execute(
        "UPDATE products SET last_in_stock_at = datetime('now') WHERE url = ?",
        (url,),
    )
    conn.commit()


# --- Retailer account operations ---

def get_retailer_accounts(user_id: int) -> dict[str, dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM retailer_accounts WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return {r["retailer"]: dict(r) for r in rows}


def set_retailer_account(user_id: int, retailer: str, email: str, password: str,
                         card_cvv: str = "", card_number: str = "",
                         card_exp_month: str = "", card_exp_year: str = "",
                         card_name: str = "", phone_last4: str = "",
                         account_last_name: str = ""):
    db = get_db()
    if password:
        db.execute(
            """INSERT INTO retailer_accounts (user_id, retailer, email, password, card_cvv,
                   card_number, card_exp_month, card_exp_year, card_name,
                   phone_last4, account_last_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, retailer) DO UPDATE SET
                 email=excluded.email, password=excluded.password,
                 card_cvv=CASE WHEN excluded.card_cvv != '' THEN excluded.card_cvv ELSE retailer_accounts.card_cvv END,
                 card_number=CASE WHEN excluded.card_number != '' THEN excluded.card_number ELSE retailer_accounts.card_number END,
                 card_exp_month=CASE WHEN excluded.card_exp_month != '' THEN excluded.card_exp_month ELSE retailer_accounts.card_exp_month END,
                 card_exp_year=CASE WHEN excluded.card_exp_year != '' THEN excluded.card_exp_year ELSE retailer_accounts.card_exp_year END,
                 card_name=CASE WHEN excluded.card_name != '' THEN excluded.card_name ELSE retailer_accounts.card_name END,
                 phone_last4=CASE WHEN excluded.phone_last4 != '' THEN excluded.phone_last4 ELSE retailer_accounts.phone_last4 END,
                 account_last_name=CASE WHEN excluded.account_last_name != '' THEN excluded.account_last_name ELSE retailer_accounts.account_last_name END""",
            (user_id, retailer, email, password, card_cvv, card_number, card_exp_month, card_exp_year, card_name, phone_last4, account_last_name),
        )
    else:
        db.execute(
            """INSERT INTO retailer_accounts (user_id, retailer, email, password, card_cvv,
                   card_number, card_exp_month, card_exp_year, card_name,
                   phone_last4, account_last_name)
               VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, retailer) DO UPDATE SET
                 email=excluded.email,
                 card_cvv=CASE WHEN excluded.card_cvv != '' THEN excluded.card_cvv ELSE retailer_accounts.card_cvv END,
                 card_number=CASE WHEN excluded.card_number != '' THEN excluded.card_number ELSE retailer_accounts.card_number END,
                 card_exp_month=CASE WHEN excluded.card_exp_month != '' THEN excluded.card_exp_month ELSE retailer_accounts.card_exp_month END,
                 card_exp_year=CASE WHEN excluded.card_exp_year != '' THEN excluded.card_exp_year ELSE retailer_accounts.card_exp_year END,
                 card_name=CASE WHEN excluded.card_name != '' THEN excluded.card_name ELSE retailer_accounts.card_name END,
                 phone_last4=CASE WHEN excluded.phone_last4 != '' THEN excluded.phone_last4 ELSE retailer_accounts.phone_last4 END,
                 account_last_name=CASE WHEN excluded.account_last_name != '' THEN excluded.account_last_name ELSE retailer_accounts.account_last_name END""",
            (user_id, retailer, email, card_cvv, card_number, card_exp_month, card_exp_year, card_name, phone_last4, account_last_name),
        )
    db.commit()


# --- Settings operations ---

def get_user_settings(user_id: int) -> dict:
    db = get_db()
    # Ensure the row exists (handles users created before settings table)
    db.execute(
        "INSERT INTO user_settings (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
        (user_id,),
    )
    db.commit()
    row = db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row)


def update_user_settings(user_id: int, poll_interval: int | None = None,
                         discord_webhook: str | None = None,
                         spend_limit: float | None = None):
    db = get_db()
    # Ensure the row exists first (handles users created before settings table)
    db.execute(
        "INSERT INTO user_settings (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
        (user_id,),
    )
    if poll_interval is not None:
        db.execute("UPDATE user_settings SET poll_interval = ? WHERE user_id = ?",
                   (poll_interval, user_id))
    if discord_webhook is not None:
        db.execute("UPDATE user_settings SET discord_webhook = ? WHERE user_id = ?",
                   (discord_webhook, user_id))
    if spend_limit is not None:
        db.execute("UPDATE user_settings SET spend_limit = ? WHERE user_id = ?",
                   (spend_limit, user_id))
    db.commit()


# --- Checkout log operations ---

def add_checkout_log(user_id: int, url: str, retailer: str, product_name: str,
                     status: str, order_number: str = "", error_message: str = "",
                     price_amount: float = 0):
    db = get_db()
    db.execute(
        """INSERT INTO checkout_log (user_id, url, retailer, product_name, status, order_number, error_message, price_amount)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, url, retailer, product_name, status, order_number, error_message, price_amount),
    )
    db.commit()


def get_checkout_log(user_id: int, limit: int = 50) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM checkout_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_user_total_spent(user_id: int) -> float:
    """Sum the price_amount of all successful checkouts for a user."""
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(SUM(price_amount), 0) as total FROM checkout_log "
        "WHERE user_id = ? AND status = 'success'",
        (user_id,),
    ).fetchone()
    return float(row["total"])


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


# --- Retailer session operations ---

def get_retailer_session(user_id: int, retailer: str) -> dict | None:
    """Get stored session (cookies + headers) for a retailer."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM retailer_sessions WHERE user_id = ? AND retailer = ?",
        (user_id, retailer),
    ).fetchone()
    return dict(row) if row else None


def set_retailer_session(user_id: int, retailer: str,
                         cookies_json: str, headers_json: str = "{}"):
    """Store session cookies and headers for a retailer."""
    db = get_db()
    db.execute(
        """INSERT INTO retailer_sessions (user_id, retailer, cookies_json, headers_json, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(user_id, retailer) DO UPDATE SET
             cookies_json=excluded.cookies_json,
             headers_json=excluded.headers_json,
             updated_at=datetime('now')""",
        (user_id, retailer, cookies_json, headers_json),
    )
    db.commit()


def delete_retailer_session(user_id: int, retailer: str):
    db = get_db()
    db.execute(
        "DELETE FROM retailer_sessions WHERE user_id = ? AND retailer = ?",
        (user_id, retailer),
    )
    db.commit()


# --- API key operations ---

def generate_api_key(user_id: int) -> str:
    """Generate and store a new API key for the user."""
    import secrets
    key = secrets.token_urlsafe(32)
    db = get_db()
    db.execute(
        "UPDATE user_settings SET api_key = ? WHERE user_id = ?",
        (key, user_id),
    )
    db.commit()
    return key


def get_user_by_api_key(api_key: str) -> dict | None:
    """Look up a user by their API key. Returns user dict or None."""
    if not api_key:
        return None
    db = get_db()
    row = db.execute(
        "SELECT u.* FROM users u JOIN user_settings s ON u.id = s.user_id "
        "WHERE s.api_key = ? AND s.api_key != ''",
        (api_key,),
    ).fetchone()
    return dict(row) if row else None


# --- OTP relay operations ---

def create_otp_request(user_id: int, retailer: str, context: str = "") -> int:
    """Create a pending OTP request. Returns the request id.

    If a pre-submitted OTP code exists (user sent the code before the request
    was created), the new request is created with that code already attached
    so the polling loop picks it up immediately.
    """
    db = get_db()
    # Expire any stale pending requests for this user+retailer
    db.execute(
        "UPDATE otp_requests SET status = 'expired' "
        "WHERE user_id = ? AND retailer = ? AND status = 'pending'",
        (user_id, retailer),
    )

    # Check for a pre-submitted code (user sent the OTP before we asked)
    pre_code = consume_presubmitted_otp(user_id)
    if pre_code:
        cursor = db.execute(
            "INSERT INTO otp_requests (user_id, retailer, context, code, status, resolved_at) "
            "VALUES (?, ?, ?, ?, 'submitted', datetime('now'))",
            (user_id, retailer, context, pre_code),
        )
    else:
        cursor = db.execute(
            "INSERT INTO otp_requests (user_id, retailer, context) VALUES (?, ?, ?)",
            (user_id, retailer, context),
        )
    db.commit()
    return cursor.lastrowid


def get_pending_otp(user_id: int, retailer: str | None = None) -> dict | None:
    """Get the most recent pending OTP request for a user (optionally filtered by retailer)."""
    db = get_db()
    if retailer:
        row = db.execute(
            "SELECT * FROM otp_requests WHERE user_id = ? AND retailer = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, retailer),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM otp_requests WHERE user_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def submit_otp_code(otp_id: int, code: str) -> bool:
    """Submit a code for a pending OTP request. Returns True if updated."""
    db = get_db()
    cursor = db.execute(
        "UPDATE otp_requests SET code = ?, status = 'submitted', resolved_at = datetime('now') "
        "WHERE id = ? AND status = 'pending'",
        (code, otp_id),
    )
    db.commit()
    return cursor.rowcount > 0


def get_otp_code(otp_id: int) -> str | None:
    """Check if an OTP code has been submitted. Returns the code or None."""
    db = get_db()
    row = db.execute(
        "SELECT code, status FROM otp_requests WHERE id = ?",
        (otp_id,),
    ).fetchone()
    if row and row["status"] == "submitted" and row["code"]:
        return row["code"]
    return None


def expire_otp_request(otp_id: int):
    """Mark an OTP request as expired."""
    db = get_db()
    db.execute(
        "UPDATE otp_requests SET status = 'expired', resolved_at = datetime('now') WHERE id = ?",
        (otp_id,),
    )
    db.commit()


def store_presubmitted_otp(user_id: int, code: str):
    """Store an OTP code that arrived before the request was created.

    Uses a special retailer='_presubmit' row. Only the most recent code is
    kept (older ones are expired). Codes older than 5 minutes are ignored
    by consume_presubmitted_otp.
    """
    db = get_db()
    # Expire any existing pre-submitted codes for this user
    db.execute(
        "UPDATE otp_requests SET status = 'expired' "
        "WHERE user_id = ? AND retailer = '_presubmit' AND status = 'submitted'",
        (user_id,),
    )
    db.execute(
        "INSERT INTO otp_requests (user_id, retailer, context, code, status, resolved_at) "
        "VALUES (?, '_presubmit', 'early_submit', ?, 'submitted', datetime('now'))",
        (user_id, code),
    )
    db.commit()


def consume_presubmitted_otp(user_id: int) -> str | None:
    """Retrieve and expire a pre-submitted OTP code (if any, within 5 minutes)."""
    db = get_db()
    row = db.execute(
        "SELECT id, code FROM otp_requests "
        "WHERE user_id = ? AND retailer = '_presubmit' AND status = 'submitted' "
        "AND created_at >= datetime('now', '-5 minutes') "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row and row["code"]:
        db.execute(
            "UPDATE otp_requests SET status = 'expired' WHERE id = ?",
            (row["id"],),
        )
        db.commit()
        return row["code"]
    return None


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
