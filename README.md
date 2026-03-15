# Pmon - Pokemon Card Stock Monitor & Auto-Checkout Bot

A personal bot for monitoring Pokemon card stock across major retailers and optionally auto-purchasing when items drop.

## Supported Retailers

- **Pokemon Center** (pokemoncenter.com)
- **Target** (target.com)
- **Best Buy** (bestbuy.com) — note: many Pokemon TCG items now use an invitation system
- **Walmart** (walmart.com)

## Features

- Real-time stock monitoring with configurable poll interval
- Discord webhook notifications when items come in stock
- Console notifications with system bell
- Auto-checkout via browser automation (Playwright)
- Web dashboard for managing products, viewing status, and triggering manual checkouts
- Persistent browser sessions (stays logged in across restarts)

## Quick Start

```bash
# 1. Install dependencies
pip install -e .

# 2. Install Playwright browsers (needed for auto-checkout)
playwright install chromium

# 3. Create your config
cp config/config.example.yaml config/config.yaml
# Edit config/config.yaml with your products and credentials

# 4. Run
pmon run
```

The dashboard will be available at `http://127.0.0.1:8888`.

## Usage

```bash
# Start with dashboard + monitoring
pmon run

# Monitor only (no auto-checkout)
pmon run --no-checkout

# Monitor only (no dashboard)
pmon run --no-dashboard

# Custom config path
pmon run --config /path/to/config.yaml

# Debug logging
pmon run -v

# Create a config file
pmon init
```

## Configuration

Copy `config/config.example.yaml` to `config/config.yaml` and edit it:

- **products**: List of product URLs to monitor
- **profiles**: Shipping/billing info for checkout
- **accounts**: Retailer login credentials
- **notifications**: Discord webhook URL
- **poll_interval**: How often to check stock (seconds, default 30)

You can also manage products and settings through the web dashboard.

## Dashboard

The web dashboard lets you:
- See real-time stock status for all monitored products
- Add/remove products
- Trigger manual checkout attempts
- View checkout history
- Update settings (poll interval, Discord webhook)

## How It Works

1. **Monitoring**: The bot polls each product URL at the configured interval, checking for stock availability via API calls and page scraping
2. **Notifications**: When a product goes from out-of-stock to in-stock, you get notified via Discord and/or console
3. **Auto-checkout**: If enabled for a product, the bot launches a browser and attempts to complete the purchase using your saved profile and credentials

## Important Notes

- **Best Buy invitation system**: Best Buy now uses an invitation-only system for many Pokemon TCG releases. The monitor will detect this and notify you, but auto-checkout won't work for invitation-only products.
- **Captchas**: Some retailers (especially Walmart) use captcha challenges. The browser runs in visible mode so you can solve captchas manually when they appear.
- **Rate limiting**: Don't set the poll interval too low or you may get IP-blocked. 30 seconds is a reasonable default.
- **Account security**: Your credentials are stored locally in `config/config.yaml` (gitignored). Never commit this file.
