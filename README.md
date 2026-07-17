# prices_bot

Posts Bitcoin and TON (Gram) prices to a Telegram channel every 3 minutes.

Python 3.12, two dependencies, no bot framework — the bot only ever calls
`sendMessage`, so there are no inbound updates to route.

## Setup

1. **Create the bot.** Telegram → [@BotFather](https://t.me/BotFather) → `/newbot`
   → copy the token.

2. **Create the channel**, then **add the bot as an administrator with "Post
   Messages"**. Plain membership is not enough — a channel is a broadcast feed
   and only admins can write to it. This is the single most common reason a
   correct bot posts nothing; you'd see
   `not enough rights to send text messages to the chat`.

3. **Get the numeric chat id.** Post anything in the channel, then:

   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
   ```

   Read `result[].channel_post.chat.id` — it looks like `-1001234567890`. Use the
   number rather than `@username`: a username can be changed at any time and would
   break the bot silently.

4. **Configure.**

   ```bash
   cp .env.example .env    # then fill in BOT_TOKEN and CHAT_ID
   ```

   `.env` is gitignored. A token that reaches a commit is burned — the only fix is
   `/revoke` in @BotFather.

5. **Check the host isn't in the US.** Binance answers HTTP 451 to US IPs:

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" https://api.binance.com/api/v3/ping   # want 200
   ```

   The bot still works from the US via its CoinGecko and Kraken fallbacks, but it
   would never use its primary source.

## Run

On the server it runs under systemd — see [deploy/prices-bot.service](deploy/prices-bot.service),
which carries the full install sequence in its header:

```bash
systemctl status prices-bot
journalctl -u prices-bot -f
```

A [Dockerfile](Dockerfile) and [docker-compose.yml](docker-compose.yml) are here too
if you ever move it to a host that prefers containers.

Locally, to iterate:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

## Checking it works

```bash
# Hit all three price providers and print what each returns
.venv/bin/python -c "import asyncio, prices; asyncio.run(prices.main_test())"
```

Set `POLL_SECONDS=10` in `.env` to watch a few posts land, then put it back.

## How it decides what to post

Three providers are tried in order — **Binance**, then **CoinGecko**, then
**Kraken** — and the first whose data passes validation wins. They come from
different companies on different infrastructure, so they fail independently.

Validation matters more than fetching here, because all three can answer HTTP 200
with data that is wrong:

- Binance serves a halted pair's last trade forever. `TONUSDT` has been frozen at
  $1.60 since 2026-07-07 while the coin trades near $1.47 — `status` is `BREAK`,
  yet `isSpotTradingAllowed` still reads `true`. The bot uses `GRAMUSDT` (the live
  pair after the June 2026 rebrand) and rejects any pair with an empty order book
  or a stale `closeTime`.
- CoinGecko silently omits ids it doesn't know and returns 200 with whatever is
  left, so the bot checks that every id it asked for came back.
- Kraken reports errors in a JSON field, with HTTP 200.

If every provider fails, the tick is skipped — the bot never posts a stale price
and never posts `$0.00`. If nothing succeeds for `WATCHDOG_SECONDS`, it exits so
the supervisor restarts it: a restart policy only fires when a process *exits*, so
a process that is alive but posting nothing has to end itself.

## A note on the coin's name

Toncoin was renamed **Gram** in June 2026. CoinGecko now returns `symbol: "gram"`
for it and Binance trades it as `GRAM*`, while Kraken still says `TON`. The label
in the channel is hardcoded as `TON (Gram)` in `render.py` and never read from a
provider — otherwise the channel would rename the coin by itself one day.

## Tuning

Everything lives in `.env`; see [.env.example](.env.example) for the full list.
The one worth knowing about: **`MIN_CHANGE_PCT`**. It ships at `0`, meaning every
tick posts — 480 messages a day, and 480 notifications for every subscriber. Raise
it (say `0.5`) and the bot only posts when a coin has actually moved that far
since its last post.
