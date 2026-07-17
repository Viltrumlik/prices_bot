"""Entrypoint: post BTC and TON (Gram) prices to a Telegram channel on a fixed
interval, forever.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

if sys.version_info < (3, 11):
    # asyncio.timeout, and TimeoutError being the builtin, both land in 3.11.
    # Ubuntu 22.04's system python3 is 3.10 and `python3 -m venv` there installs
    # and imports cleanly, only to fail on the first tick.
    raise SystemExit(f"Python 3.11+ required, this is {sys.version.split()[0]}")

import httpx

import config
from prices import FetchError, Quote, Reference, fetch_quotes
from render import render
from tg import FatalTelegramError, TelegramError, send_message

log = logging.getLogger("bot")

_MASK = "<token>"


class _RedactingFormatter(logging.Formatter):
    """Strip the bot token out of every log line.

    The token sits in the Telegram URL path and httpx logs each request URL, so
    without this it lands in `docker compose logs` in plaintext — and from there
    into whatever log paste someone makes when asking for help. Redacting in the
    formatter covers messages, args and tracebacks in one place, whoever logged
    them.
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text.replace(config.BOT_TOKEN, _MASK) if config.BOT_TOKEN else text


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        _RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Nearly a thousand request lines a day, every one carrying the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _should_post(quotes: dict[str, Quote], last_posted: dict[str, Quote]) -> bool:
    if config.MIN_CHANGE_PCT <= 0 or not last_posted:
        return True
    # Measured against what the channel last saw, not the last tick: comparing
    # tick-to-tick would mean a steady drift never clears the threshold and the
    # bot goes quiet however far the price ends up travelling.
    for symbol, quote in quotes.items():
        prev = last_posted.get(symbol)
        if prev is None or prev.price <= 0:
            return True
        move = abs(quote.price - prev.price) / prev.price * 100
        if move >= config.MIN_CHANGE_PCT:
            return True
    return False


async def _tick(
    client: httpx.AsyncClient,
    reference: Reference | None,
    last_posted: dict[str, Quote],
) -> tuple[Reference, dict[str, Quote]]:
    """One fetch-and-post. Returns the updated (reference, last_posted)."""
    quotes = await fetch_quotes(client, reference)
    # Advance the jump reference on every observation, posted or not: it exists to
    # catch a provider glitching between ticks, not to track what the channel saw.
    reference = Reference(quotes=quotes, at=time.monotonic())

    if _should_post(quotes, last_posted):
        await send_message(client, render(quotes))
        log.info(
            "posted %s",
            "  ".join(f"{s}=${q.price}" for s, q in sorted(quotes.items())),
        )
        last_posted = quotes
    else:
        log.info("no move above MIN_CHANGE_PCT=%s, skipped", config.MIN_CHANGE_PCT)

    return reference, last_posted


async def main() -> None:
    _setup_logging()
    config.validate()
    log.info("posting to %s every %ss", config.CHAT_ID, config.POLL_SECONDS)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        stop.set()
        # Hand the signals back to Python's defaults, so a second Ctrl-C or
        # SIGTERM kills the process outright rather than waiting out a tick
        # that may still have a minute of provider timeouts left in it.
        for s in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(s)

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Without this, python is PID 1 under Docker, SIGTERM has no handler and
        # is ignored, and every `docker compose down` waits out the 10s grace
        # period before SIGKILL. systemd's SIGTERM has the same problem.
        loop.add_signal_handler(sig, _on_signal)

    timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    reference: Reference | None = None
    last_posted: dict[str, Quote] = {}
    last_ok = time.monotonic()
    next_at = time.monotonic()

    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": "prices-bot/1.0"}
    ) as client:
        while not stop.is_set():
            next_at += config.POLL_SECONDS

            try:
                # A hard ceiling on the tick, so no pile-up of provider retries
                # and rate-limit waits can outlive the interval itself.
                async with asyncio.timeout(config.TICK_BUDGET):
                    reference, last_posted = await _tick(client, reference, last_posted)
                # A tick suppressed by MIN_CHANGE_PCT still counts: the bot is
                # working, just quiet, and the watchdog must not restart it for
                # a flat market.
                last_ok = time.monotonic()
            except FatalTelegramError:
                log.exception(
                    "fatal Telegram error — check the token, and that the bot is a "
                    "channel administrator with Post Messages"
                )
                raise SystemExit(1)
            except TimeoutError:
                log.warning("tick exceeded its %.0fs budget", config.TICK_BUDGET)
            except (FetchError, TelegramError):
                # Skip the tick rather than post a stale or partial price. The next
                # one is POLL_SECONDS away with fresher data anyway.
                log.exception("tick failed")
            except Exception:
                log.exception("unexpected error in tick")

            idle = time.monotonic() - last_ok
            if config.WATCHDOG_SECONDS and idle > config.WATCHDOG_SECONDS:
                # A supervisor's restart policy only fires when the process exits,
                # so a process that is alive but posting nothing has to exit itself.
                log.error(
                    "no successful tick in %.0fs — exiting so the supervisor restarts us",
                    idle,
                )
                raise SystemExit(1)

            sleep_for = next_at - time.monotonic()
            if sleep_for < 0:
                # Ticks ran long. Skip the slots we missed rather than fire a
                # catch-up burst at the API.
                missed = int(-sleep_for // config.POLL_SECONDS) + 1
                log.warning("running %.0fs behind, skipping %d tick(s)", -sleep_for, missed)
                next_at += missed * config.POLL_SECONDS
                sleep_for = next_at - time.monotonic()

            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, sleep_for))
            except TimeoutError:
                pass  # the normal path: slept the full interval, no signal

    log.info("stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
