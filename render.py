"""Channel message formatting.

HTML rather than MarkdownV2: MarkdownV2 requires escaping `.`, `-`, `+` and `(`,
every one of which occurs in a formatted price or a percent change.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from decimal import Decimal

from prices import Quote

# Labels are fixed here and never read from the API. CoinGecko already reports
# symbol="gram" for Toncoin after the June 2026 rebrand, so echoing the provider's
# own symbol would silently rename the coin in the channel one day.
LABELS = {
    "BTC": ("🟠", "BTC"),
    "TON": ("💎", "TON (Gram)"),
}
ORDER = ("BTC", "TON")


def _fmt_price(price: Decimal) -> str:
    if price >= 1000:
        rounded = price.quantize(Decimal("0.01"))
    elif price >= 1:
        rounded = price.quantize(Decimal("0.0001"))
    else:
        rounded = price.quantize(Decimal("0.000001"))
    return f"{rounded:,}"


def _fmt_change(change: Decimal) -> str:
    marker = "🟢" if change >= 0 else "🔴"
    return f"{marker} {change:+.2f}%"


def render(quotes: dict[str, Quote]) -> str:
    lines = ["📊 <b>Crypto Prices</b>", ""]

    for symbol in ORDER:
        quote = quotes.get(symbol)
        if quote is None:
            continue
        emoji, label = LABELS[symbol]
        price = html.escape(_fmt_price(quote.price))
        # The window is spelled out because it is not always 24h — Kraken has no
        # rolling-24h open, and an unlabelled percent would read as one anyway.
        window = html.escape(quote.change_window)
        lines.append(
            f"{emoji} <b>{html.escape(label)}</b>  ${price}  "
            f"{_fmt_change(quote.change)} ({window})"
        )

    lines.append("")
    lines.append(f"🕐 <i>Updated: {datetime.now(timezone.utc):%H:%M} UTC</i>")
    return "\n".join(lines)
