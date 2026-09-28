#!/usr/bin/env python3
"""Register the Telegram webhook for this bot.

Reads BOT_TOKEN and (optionally) WEBHOOK_SECRET from the environment.
The webhook URL comes from the first CLI argument, falling back to
WEBHOOK_URL from the environment.

Usage:
    python set_webhook.py https://myhost.example/webhook
    WEBHOOK_URL=https://myhost.example/webhook python set_webhook.py
"""
from __future__ import annotations

import os
import sys

import httpx


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    url = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WEBHOOK_URL", "")).strip()
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()

    if not token:
        sys.exit("error: BOT_TOKEN is not set")
    if not url:
        sys.exit("error: pass the webhook URL as an argument or set WEBHOOK_URL")

    payload: dict[str, str] = {"url": url}
    if secret:
        payload["secret_token"] = secret

    resp = httpx.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json=payload,
        timeout=30,
    )
    print(resp.status_code, resp.text)
    resp.raise_for_status()


if __name__ == "__main__":
    main()
