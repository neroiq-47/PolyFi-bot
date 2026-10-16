# PolyFi Bot — research and plan

Research done 16 Oct 2026 against Polymarket's current docs and the official `polymarket-client` 0.13 SDK.

## What you asked for vs. what exists

| Ask | What's possible today | What PolyFi does |
|---|---|---|
| **1-minute BTC betting** | Polymarket has **no 1-minute market**. The shortest are BTC Up/Down **5-minute** markets (launched Feb 2026), plus 15-minute ones. 1-minute markets were mentioned as planned, but none have launched. | The bot trades the **5-minute** market. It re-evaluates **every second**, and its volatility model is built from **1-minute candles**. If 1-minute markets launch, set `POLYFI_WINDOW_SECONDS=60`. |
| **Free only / no fees** | Crypto markets charge a **taker** fee: `fee = shares × 0.07 × p × (1−p)`, which is up to $1.75 per 100 shares at 50¢ (about 3.5% of the money spent). **Makers pay $0** and also earn daily rebates. | `FREE_ONLY=true` (the default) places only **post-only limit orders**. They can never take liquidity, so they never pay a fee. The trade-off: some orders won't fill. |
| **Gasless transactions** | Polymarket's **Relayer** pays the gas for wallet actions on Deposit Wallets: approvals, transfers, redeeming winnings. Orders are signed off-chain and cost no gas. | Uses `AsyncSecureClient` with a **Relayer API key**, so approvals, redemptions and withdrawals are all gasless. |
| **No-fee withdrawals** | Polymarket says withdrawals are "instant and free". The bridge converts pUSD to USDC. | One-click withdraw to **native USDC on Polygon**, e.g. to your Coinbase USDC address. In Coinbase, USDC→USD (1:1) and an ACH bank withdrawal are free. |
| **Apple Pay deposit** | Apple Pay only runs through card on-ramps: Polymarket's MoonPay (≈3–5%) or Coinbase (≈2.5% for card; zero-fee USDC on-ramp is for approved partners only). Coinbase's hosted guest Apple Pay checkout was deprecated on 30 Jun 2026. | The dashboard has an **Apple Pay** button that opens Polymarket's own deposit flow, with its fee labelled. It also offers a **$0 route**: bank → Coinbase → buy USDC (1:1) → send on **Polygon** to your bridge deposit address, which the dashboard shows. |

So a truly free path exists end to end (bank → Coinbase → Polymarket → maker-only trades → withdraw → Coinbase → bank), but **Apple Pay itself is never free**.

## Architecture

```
Coinbase BTC-USD (1s spot, 1m candles) ─┐
                                        ├─► Engine (1s tick) ─► Gateway ─► Polymarket CLOB / Relayer / Bridge
Polymarket Gamma (btc-updown-5m-<t>) ───┘        │                       (demo | paper | live)
                                                 ├─► SQLite trade journal
                                                 └─► FastAPI ─► Dashboard (purple / blue / mint)
```

* **Market discovery.** The slug is `btc-updown-5m-<unix window start>`, looked up with `client.get_market(slug=…)`. Up and Down are matched by outcome label.
* **Fair value.** `P(Up) = Φ( ln(S/S₀) / (σ·√τ) )`. Here S is BTC now, S₀ is the price to beat (the window's opening price), σ is per-second volatility from the last 60 one-minute candles, and τ is the seconds left. Ties resolve Up.
* **Entry.** Only between `entry_after_s` (20s) and `stop_before_end_s` (60s before close), with at most **one position per window**.
  * In free mode, the bot bids one tick above the best bid. It never crosses the ask, and it caps its price at `fair − min_edge`.
  * In taker mode, it buys at the ask only when `fair − ask − fee ≥ min_edge`.
* **Management.** A resting order is cancelled if the edge halves or the window is about to close. Positions are held to resolution, and winnings are redeemed gaslessly after each window.
* **Risk.** The daily loss limit halts the bot. There is a fixed stake per trade and a balance check before every order. Pausing cancels resting orders.

## Honest risk notes

* **5-minute crypto markets are dominated by fast professional bots.** Several write-ups report that books thin out and prices jump in the final ~90s, which is why the bot stops at T-60s. Published "win rate" claims are small-sample anecdotes.
* **Our signal uses Coinbase spot. Polymarket resolves on Chainlink's BTC/USD stream.** They are close, but not identical. Always read the market's Rules.
* **Maker-only means adverse selection.** You tend to get filled exactly when the price moves against you. Run `paper` mode for days before `live`.
* **Polymarket's international exchange is geoblocked in some countries,** including for US persons, who use the separate CFTC-regulated Polymarket US. PolyFi does **not** route around geoblocks. Only run `live` where it is permitted.

## Roadmap

1. ✅ Demo, paper and live modes; maker-only "free" mode; gasless approvals, redeem and withdraw; dashboard.
2. Swap REST polling for WebSocket streams (`client.subscribe([MarketSpec(...)])`) to cut latency.
3. Use the Chainlink BTC/USD feed for S₀ and S, to match resolution exactly.
4. Add 15-minute markets and ETH/SOL with the same engine (`POLYFI_WINDOW_SECONDS=900`, `POLYFI_ASSET=eth`).
5. Use session keys (scoped, time-limited signer) so the main wallet key never sits on the bot server.
6. Add a backtest harness on recorded books and fills.

## Sources

* Polymarket docs: [Fees](https://docs.polymarket.com/trading/fees) · [Wallets & gasless](https://docs.polymarket.com/trading/wallets-auth) · [Python SDK](https://docs.polymarket.com/getting-started/python) · [Deposit](https://docs.polymarket.com/trading/bridge/deposit) · [Withdraw](https://docs.polymarket.com/trading/bridge/withdraw) · [Builder tiers](https://docs.polymarket.com/programs/builders/tiers)
* 5-minute markets: [CoinMarketCap](https://coinmarketcap.com/academy/article/polymarket-debuts-5-minute-bitcoin-prediction-markets-with-instant-settlement) · [Fortune](https://dc.fortune.com/2026/03/16/betting-on-5-minute-swings-on-bitcoin-price-are-the-hot-new-thing-on-prediction-markets) · [Bitquery](https://bitquery.io/investigations/polymarket-five-minute-casino)
* Fee rollout: [The Block](https://www.theblock.co/post/384461/polymarket-adds-taker-fees-to-15-minute-crypto-markets-to-fund-liquidity-rebates) · [Pine Analytics](https://pineanalytics.substack.com/p/polymarket-fee-rollout)
* Deposits / Apple Pay: [Coinbase Onramp FAQ](https://docs.cdp.coinbase.com/onramp/additional-resources/faq.md) · [predictionmarkets101](https://predictionmarkets101.com/how-to-deposit-on-polymarket)
* Slug format and resolution: [polyguana example](https://polyguana.com/events/btc-updown-5m-1773879000) · [KuCoin guide](https://www.kucoin.com/support/48142946142410)
