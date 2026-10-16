# PolyFi Bot

A simple, **fee-free (maker-only)**, **gasless** Polymarket bot for the **BTC Up/Down 5-minute** markets. It has a clean purple/blue/mint dashboard.

> Polymarket has no 1-minute market. The shortest is 5 minutes. The bot ticks every second and models volatility from 1-minute candles. See [docs/PLAN.md](docs/PLAN.md) for the full research, the fee math and the risks.

## Quick start (demo, no keys, works offline)

```bash
pip install -e ".[dev]"
python -m polyfi            # → http://127.0.0.1:8787
```

Press **Start bot**. Demo mode simulates both BTC and the order book.

## Modes

| `POLYFI_MODE` | BTC price | Polymarket books | Orders |
|---|---|---|---|
| `demo` (default) | simulated | simulated | simulated |
| `paper` | Coinbase live | Polymarket live | simulated fills |
| `live` | Coinbase live | Polymarket live | **real**, gasless |

## Going live

1. Create a Polymarket account, then go to **Settings → API Keys → Relayer API Keys** and create a key. Copy the signer address and the key.
2. `cp .env.example .env` and fill in `SIGNER_PRIVATE_KEY`, `POLYMARKET_RELAYER_API_KEY` and `POLYMARKET_RELAYER_API_KEY_ADDRESS`. Fill in `POLYMARKET_WALLET_ADDRESS` too if you are connecting an existing account.
3. Run `paper` mode for a while first. Then set `POLYFI_MODE=live`.
4. Fund the account:
   * **Free:** Coinbase → buy USDC with your bank balance → send on **Polygon** to the deposit address shown in the dashboard.
   * **Apple Pay:** use the dashboard button. It costs about 2.5–5% in card processor fees.

**Safer key setup (optional):** a session key lets the bot trade but **not withdraw**. Run `python -m polyfi.session_key authorize` once on a trusted machine. It reads the owner key and Builder API key from `.env.owner`, and session keys are in beta, so Polymarket must enable your Builder key first by email. Put the printed `POLYMARKET_SESSION_PRIVATE_KEY` in the bot's `.env`. In this mode you collect winnings and withdraw from polymarket.com. To cut the bot off, run `python -m polyfi.session_key revoke <address>`.

Only run live where Polymarket is permitted for you. The bot does not bypass geoblocks.

## Live data

* **Order books** stream over Polymarket's WebSocket. If the stream goes quiet for more than 5 seconds, the bot falls back to REST.
* **BTC price:** live mode uses **Chainlink BTC/USD** from Polymarket's price stream, which is the source these markets settle on. Coinbase fills any gaps. The dashboard shows which source is in use.

## Backtesting

Paper and live modes record every tick (BTC price, price to beat, both books) to SQLite. Replay them through the same engine with different settings:

```bash
python -m polyfi.backtest --db polyfi.sqlite3 --sweep        # min-edge × maker/taker grid
python -m polyfi.backtest --db polyfi.sqlite3 --min-edge 0.05
```

Maker fills are simulated optimistically, because the replay can't see your place in the order queue. Treat maker results as an upper bound.

## Free-only mode

With **Free only** on (the default), every order is a **post-only limit order**. Makers pay **$0** in fees on Polymarket and earn daily rebates. With it off, the bot may take the ask and pays the crypto taker fee: `shares × 0.07 × p × (1−p)`. The trade journal records every fee.

## Safety

* Fixed stake per trade, one position per 5-minute window, and a daily loss limit that halts the bot.
* Pausing the bot cancels any resting order.
* The dashboard binds to `127.0.0.1`. To expose it, you must set `POLYFI_DASHBOARD_TOKEN`.
* Keys stay server-side. `/api/state` never returns them.

## Tests

```bash
pytest
```

This is not financial advice. Short-dated crypto markets are fast and competitive, and you can lose your whole stake.
