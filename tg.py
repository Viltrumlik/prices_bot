"""Telegram sendMessage.

The bot has no inbound surface — it posts and nothing else — so it calls the one
HTTP endpoint it needs directly rather than carrying a bot framework whose reason
to exist is routing incoming updates.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

import config

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"

# Longer than this and there is no point sleeping it off inside the tick: the
# wait would blind the watchdog and achieve nothing the next tick won't do with
# fresher prices.
MAX_RETRY_AFTER = 60.0


class TelegramError(Exception):
    """Transient. Skip the tick and try again on the next one."""


class FatalTelegramError(Exception):
    """Needs a human: a bad token or the bot losing its channel rights."""


async def send_message(client: httpx.AsyncClient, text: str) -> int:
    payload = {
        "chat_id": config.CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }

    for attempt in (1, 2):
        try:
            response = await client.post(API.format(token=config.BOT_TOKEN), json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == 2:
                raise TelegramError(f"{type(e).__name__}: {e}") from e
            await asyncio.sleep(2)
            continue

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code == 200 and data.get("ok"):
            return data["result"]["message_id"]

        code = data.get("error_code", response.status_code)
        description = data.get("description") or response.text[:200]

        if code in (401, 403, 404):
            # None of these get better on their own: 401 is an invalid or revoked
            # token, 403 is the bot lacking rights in the channel, and 404 means
            # the token is malformed (a bad chat_id is a 400, not a 404).
            raise FatalTelegramError(f"HTTP {code}: {description}")

        if code == 429:
            # retry_after is documented as optional, so it may be absent — and
            # nothing guarantees it is a number when it is present.
            try:
                wait = float((data.get("parameters") or {}).get("retry_after", 5))
            except (TypeError, ValueError):
                wait = 5.0
            if wait > MAX_RETRY_AFTER or attempt == 2:
                raise TelegramError(f"rate limited for {wait:.0f}s: {description}")
            log.warning("telegram rate limited, waiting %.0fs", wait)
            await asyncio.sleep(wait)
            continue

        if code >= 500:
            if attempt == 2:
                raise TelegramError(f"HTTP {code}: {description}")
            await asyncio.sleep(2)
            continue

        raise TelegramError(f"HTTP {code}: {description}")

    raise TelegramError("exhausted retries")
