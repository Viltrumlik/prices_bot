"""Environment-backed settings.

Importing this module must stay side-effect free apart from reading the
environment, so `prices.py` can be exercised standalone without a bot token.
Call `validate()` from the entrypoint instead.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

# override=False: real environment variables always beat a stray .env, so the
# same code path works locally and under Docker/systemd with no branching.
load_dotenv()


def _num(name: str, default: str, cast):
    # `POLL_SECONDS=` in a .env is a typo, not a request for the default, but
    # os.environ.get would hand back "" and cast would die without naming it.
    raw = os.environ.get(name) or default
    try:
        value = cast(raw)
    except (ValueError, InvalidOperation):
        raise SystemExit(f"{name}={raw!r} is not a valid number") from None
    # Decimal("nan") and Decimal("inf") cast without complaint and only blow up
    # later, when validate() tries to order them.
    if isinstance(value, Decimal) and not value.is_finite():
        raise SystemExit(f"{name}={raw!r} is not a finite number")
    return value


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

POLL_SECONDS = _num("POLL_SECONDS", "180", int)
WATCHDOG_SECONDS = _num("WATCHDOG_SECONDS", "1800", int)
MIN_CHANGE_PCT = _num("MIN_CHANGE_PCT", "0", Decimal)
STALE_AFTER = _num("STALE_AFTER", "300", int)
MAX_JUMP_PCT = _num("MAX_JUMP_PCT", "20", Decimal)

# The jump check only says anything while its reference really is the previous
# tick. Past this age it would be comparing against an arbitrarily old price.
JUMP_REFERENCE_MAX_AGE = POLL_SECONDS * 1.5

# A tick that outlives this is abandoned, so no combination of provider retries
# and rate-limit waits can stack up into an overlapping tick.
TICK_BUDGET = max(20.0, POLL_SECONDS * 0.8)


def validate() -> None:
    missing = [name for name in ("BOT_TOKEN", "CHAT_ID") if not globals()[name]]
    if missing:
        raise SystemExit(
            f"{', '.join(missing)} not set — copy .env.example to .env and fill it in"
        )

    # Without these, a bad value doesn't fail here — it turns into a crashloop or
    # a divide-by-zero deep in the loop, hours later, with a worse error message.
    for name, value, minimum in (
        ("POLL_SECONDS", POLL_SECONDS, 1),
        ("STALE_AFTER", STALE_AFTER, 1),
        ("MAX_JUMP_PCT", MAX_JUMP_PCT, Decimal("0.01")),
    ):
        if value < minimum:
            raise SystemExit(f"{name}={value} is below the minimum of {minimum}")

    # Upper bound too: a threshold above 100% is unreachable, and the bot would
    # post once at startup and then stay silent forever without complaining.
    if not 0 <= MIN_CHANGE_PCT <= 100:
        raise SystemExit(f"MIN_CHANGE_PCT={MIN_CHANGE_PCT} must be between 0 and 100")

    if WATCHDOG_SECONDS and WATCHDOG_SECONDS < 2 * POLL_SECONDS:
        raise SystemExit(
            f"WATCHDOG_SECONDS={WATCHDOG_SECONDS} must be 0 (disabled) or at least "
            f"twice POLL_SECONDS={POLL_SECONDS} — otherwise the bot restarts itself "
            f"faster than it can post"
        )
