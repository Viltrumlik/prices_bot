"""Price fetching with three independent providers and per-provider validation.

The providers are tried in order and the first one whose data passes validation
wins. They fail independently: different companies, different infrastructure,
different symbol names for the same coin.

Validation is the point of this module, not an extra. Every provider here can
answer HTTP 200 with data that is wrong:

  * Binance keeps serving a halted pair's last trade forever. TONUSDT has been
    frozen at $1.60 since 2026-07-07 while the coin trades at ~$1.47 — status is
    BREAK but isSpotTradingAllowed still reads true. Only the empty order book
    and the stale closeTime give it away.
  * CoinGecko drops ids it does not recognise and returns 200 with the rest, so
    a typo'd id silently halves the payload instead of erroring.
  * Kraken reports its errors in a JSON field and still returns 200.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

import config

log = logging.getLogger(__name__)

BTC = "BTC"
TON = "TON"

# Toncoin rebranded to "Gram" in June 2026. Binance halted every TON* pair and
# relisted as GRAM*; CoinGecko kept the id but now reports symbol="gram"; Kraken
# still uses TON. These are the live names as of 2026-07-17 — the label shown in
# the channel is fixed separately in render.py.
BINANCE_PAIRS = {"BTCUSDT": BTC, "GRAMUSDT": TON}
CG_IDS = {"bitcoin": BTC, "the-open-network": TON}
KRAKEN_PAIRS = {"XXBTZUSD": BTC, "TONUSD": TON}


# Nothing this bot tracks trades within several orders of magnitude of this. A
# finite-but-absurd price is a corrupt feed, and it has to be caught here: past
# validation there is no fallback left, and Decimal.quantize raises on anything
# near its 28-digit context limit, so render would take the whole tick down.
MAX_PLAUSIBLE_PRICE = Decimal("1e9")


class FetchError(Exception):
    """A provider did not return usable data. Try the next one."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    change: Decimal
    # Which window `change` covers. Not every provider offers a rolling 24h.
    change_window: str
    ts: int  # unix seconds the price was last updated at the source


@dataclass(frozen=True)
class Reference:
    """The last quotes we accepted, and when — the jump check's baseline."""

    quotes: dict[str, Quote]
    at: float  # time.monotonic()


def _dec(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise FetchError(f"{field}: not a number: {value!r}") from e
    # Decimal happily parses "NaN" and "Infinity", and ordering a NaN afterwards
    # raises InvalidOperation — an ArithmeticError, not a FetchError, which would
    # escape the per-provider handler and take the whole tick down with it.
    if not number.is_finite():
        raise FetchError(f"{field}: not a finite number: {value!r}")
    return number


def _retry_after(response: httpx.Response, default: float) -> float:
    try:
        wait = float(response.headers.get("Retry-After", default))
    except (TypeError, ValueError):
        return default
    # Capped so a hostile value cannot stall us past the next tick.
    return max(0.0, min(60.0, wait))


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    attempts: int = 3,
) -> object:
    """GET with bounded retries.

    A 4xx other than 429 means our request is wrong and will never succeed, so it
    fails immediately. 5xx and transport errors back off. A 429 waits exactly as
    long as the server asked: blind retries against Binance escalate a 429 into
    an HTTP 418 IP ban lasting from two minutes to three days.
    """
    delay = 1.0
    last_error: FetchError | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = FetchError(f"{url}: {type(e).__name__}: {e}")
        else:
            status = response.status_code
            if status == 429:
                last_error = FetchError(f"{url}: rate limited")
                if attempt < attempts:
                    wait = _retry_after(response, delay)
                    log.warning("429 from %s, waiting %.0fs", response.url.host, wait)
                    await asyncio.sleep(wait)
                    delay *= 2  # `continue` skips the escalation at the tail
                continue
            if status >= 500:
                last_error = FetchError(f"{url}: HTTP {status}")
            elif status >= 400:
                raise FetchError(f"{url}: HTTP {status} — bad request, not retrying")
            else:
                try:
                    return response.json()
                except ValueError as e:
                    raise FetchError(f"{url}: invalid JSON: {e}") from e

        if attempt < attempts:
            await asyncio.sleep(delay + random.uniform(0, 0.3))
            delay *= 2

    raise last_error or FetchError(f"{url}: failed after {attempts} attempts")


async def _binance(client: httpx.AsyncClient) -> dict[str, Quote]:
    symbols = json.dumps(list(BINANCE_PAIRS), separators=(",", ":"))
    data = await _get_json(
        client, "https://api.binance.com/api/v3/ticker/24hr", {"symbols": symbols}
    )
    if not isinstance(data, list):
        raise FetchError(f"binance: expected a list, got {type(data).__name__}")

    tickers = {t.get("symbol"): t for t in data if isinstance(t, dict)}
    quotes = {}
    for pair, symbol in BINANCE_PAIRS.items():
        ticker = tickers.get(pair)
        if ticker is None:
            raise FetchError(f"binance: {pair} missing from response")
        # A halted pair still reports a lastPrice; the empty book is the tell.
        if _dec(ticker.get("bidPrice"), f"binance {pair} bidPrice") <= 0:
            raise FetchError(f"binance: {pair} has an empty order book (halted?)")
        quotes[symbol] = Quote(
            symbol=symbol,
            price=_dec(ticker.get("lastPrice"), f"binance {pair} lastPrice"),
            change=_dec(
                ticker.get("priceChangePercent"), f"binance {pair} priceChangePercent"
            ),
            change_window="24h",
            ts=int(_dec(ticker.get("closeTime"), f"binance {pair} closeTime")) // 1000,
        )
    return quotes


async def _coingecko(client: httpx.AsyncClient) -> dict[str, Quote]:
    data = await _get_json(
        client,
        "https://api.coingecko.com/api/v3/simple/price",
        {
            "ids": ",".join(CG_IDS),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        },
    )
    if not isinstance(data, dict):
        raise FetchError(f"coingecko: expected an object, got {type(data).__name__}")

    quotes = {}
    for coin_id, symbol in CG_IDS.items():
        # An unknown id is omitted rather than rejected, so the status code says
        # nothing about whether we got what we asked for.
        row = data.get(coin_id)
        if not isinstance(row, dict):
            raise FetchError(f"coingecko: {coin_id} missing from response")
        quotes[symbol] = Quote(
            symbol=symbol,
            price=_dec(row.get("usd"), f"coingecko {coin_id} usd"),
            change=_dec(row.get("usd_24h_change"), f"coingecko {coin_id} change"),
            change_window="24h",
            ts=int(_dec(row.get("last_updated_at"), f"coingecko {coin_id} updated_at")),
        )
    return quotes


async def _kraken(client: httpx.AsyncClient) -> dict[str, Quote]:
    data = await _get_json(
        client, "https://api.kraken.com/0/public/Ticker", {"pair": "XBTUSD,TONUSD"}
    )
    if not isinstance(data, dict):
        raise FetchError(f"kraken: expected an object, got {type(data).__name__}")
    # Errors arrive with HTTP 200; this array is the only signal.
    if data.get("error"):
        raise FetchError(f"kraken: {data['error']}")

    result = data.get("result") or {}
    quotes = {}
    for pair, symbol in KRAKEN_PAIRS.items():
        ticker = result.get(pair)
        if not isinstance(ticker, dict):
            raise FetchError(f"kraken: {pair} missing from response")
        try:
            last = _dec(ticker["c"][0], f"kraken {pair} last")
            bid = _dec(ticker["b"][0], f"kraken {pair} bid")
            opening = _dec(ticker["o"], f"kraken {pair} open")
        except (KeyError, IndexError, TypeError) as e:
            raise FetchError(f"kraken: {pair} has an unexpected shape: {e}") from e
        if bid <= 0:
            raise FetchError(f"kraken: {pair} has no bid")
        # `o` is today's 00:00 UTC open — Kraken publishes no rolling-24h open, so
        # this is a since-midnight change and says so via change_window rather
        # than being passed off as the 24h figure the other two return.
        change = (last - opening) / opening * 100 if opening > 0 else Decimal(0)
        # Kraken exposes no feed timestamp either, so there is nothing for the
        # staleness check to bite on; the bid/last checks carry it alone.
        quotes[symbol] = Quote(
            symbol=symbol,
            price=last,
            change=change,
            change_window="today",
            ts=int(time.time()),
        )
    return quotes


PROVIDERS = (("binance", _binance), ("coingecko", _coingecko), ("kraken", _kraken))


def _validate(name: str, quotes: dict[str, Quote], reference: Reference | None) -> None:
    now = int(time.time())

    # The jump check is only meaningful while the reference really is the previous
    # tick. Without this, a run of rejections freezes the reference and every
    # later tick is measured against it — the gap only widens, so the check
    # rejects every real price from then on and the channel goes dark until the
    # watchdog restarts the process.
    check_jump = (
        reference is not None
        and time.monotonic() - reference.at <= config.JUMP_REFERENCE_MAX_AGE
    )

    for symbol, quote in quotes.items():
        if quote.price <= 0 or quote.price > MAX_PLAUSIBLE_PRICE:
            raise FetchError(f"{name}: {symbol} price is implausible: {quote.price}")

        age = now - quote.ts
        if age > config.STALE_AFTER:
            raise FetchError(
                f"{name}: {symbol} is {age}s stale — frozen feed, "
                f"or is this machine's clock ahead?"
            )

        if not check_jump:
            continue
        prev = reference.quotes.get(symbol)
        if prev is not None and prev.price > 0:
            jump = abs(quote.price - prev.price) / prev.price * 100
            if jump > config.MAX_JUMP_PCT:
                raise FetchError(
                    f"{name}: {symbol} jumped {jump:.1f}% since the last tick "
                    f"({prev.price} -> {quote.price})"
                )


def _provider_budget() -> float:
    """How long any one provider may hold the tick.

    Each provider gets a slice, and the leftover slice covers the Telegram post.
    Without a slice, a provider answering 429 with a 60s Retry-After spends the
    whole tick asleep and the fallbacks — the entire reason there are three —
    are never reached. Note the perverse ordering this protects against: a 418
    IP ban falls through in milliseconds and the bot keeps working, so it is the
    milder, transient 429 that would otherwise do the real damage.
    """
    return config.TICK_BUDGET / (len(PROVIDERS) + 1)


async def fetch_quotes(
    client: httpx.AsyncClient, reference: Reference | None = None
) -> dict[str, Quote]:
    """Return quotes from the first provider whose data passes validation."""
    failures = []
    budget = _provider_budget()

    for name, provider in PROVIDERS:
        try:
            async with asyncio.timeout(budget):
                quotes = await provider(client)
            _validate(name, quotes, reference)
        except TimeoutError:
            log.warning("provider %s used up its %.0fs slice", name, budget)
            failures.append(f"{name}: exceeded {budget:.0f}s")
            continue
        except (FetchError, ArithmeticError) as e:
            # ArithmeticError too: no single provider's bad number should be able
            # to abort the chain before the others have been tried.
            log.warning("provider %s rejected: %s", name, e)
            failures.append(f"{name}: {e}")
            continue
        if name != PROVIDERS[0][0]:
            log.info("using fallback provider %s", name)
        return quotes

    raise FetchError("all providers failed — " + "; ".join(failures))


async def main_test() -> None:
    """Exercise every provider independently.

    python3 -c "import asyncio, prices; asyncio.run(prices.main_test())"
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for name, provider in PROVIDERS:
            try:
                quotes = await provider(client)
                _validate(name, quotes, None)
            except (FetchError, ArithmeticError) as e:
                print(f"{name:11} FAILED: {e}")
                continue
            for symbol, quote in sorted(quotes.items()):
                age = int(time.time()) - quote.ts
                print(
                    f"{name:11} {symbol:4} ${quote.price:>12} "
                    f"{quote.change:+7.2f}% ({quote.change_window})  age={age}s"
                )
