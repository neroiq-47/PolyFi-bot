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

Only run live where Polymarket is permitted for you. The bot does not bypass geoblocks.

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
