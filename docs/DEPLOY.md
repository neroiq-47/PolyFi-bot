# Deploy to Cloudflare

PolyFi runs on **Cloudflare Containers**. A small Worker (`worker/index.ts`) sits in front of one always-on container that runs the bot and dashboard (`Dockerfile`). A cron trigger pings the container every minute so it never sleeps.

## Cost

You need the **Workers Paid plan ($5/mo)**. The default `basic` instance (¼ vCPU, 1 GiB) running 24/7 uses about 720 GiB-hours of memory a month. The plan includes 25 GiB-hours, so expect roughly **$12/mo in total**. `lite` (256 MiB) comes to about $7/mo, but it may be too small for Python plus the Polymarket SDK. Prices come from [developers.cloudflare.com/containers/pricing](https://developers.cloudflare.com/containers/pricing/).

## Option A: from your computer (Docker running)

```bash
npm ci
npx wrangler login
npx wrangler secret put POLYFI_DASHBOARD_TOKEN      # required: any long random string
npx wrangler deploy
```

Open the `https://polyfi-bot.<your-subdomain>.workers.dev` URL that wrangler prints. Enter your dashboard token when prompted.

## Option B: from GitHub (no local Docker)

1. Create a Cloudflare API token with the **Edit Cloudflare Workers** template, plus **Account → Containers: Edit**. Note your account ID.
2. In the GitHub repo, go to **Settings → Secrets and variables → Actions** and add `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` and `POLYFI_DASHBOARD_TOKEN`. For live mode, also add your Polymarket keys, using the same names as `.env.example`.
3. Go to **Actions → Deploy to Cloudflare → Run workflow**.

## Option C: Cloudflare's Git integration (Workers Builds)

If you connected this repo in the Cloudflare dashboard, go to **Workers & Pages → polyfi-bot → Settings → Build** and check three things:

1. **API token: add Containers access.** The token Workers Builds creates by default only has Account Settings (read), Workers Scripts, KV and R2 (edit). Wrangler also has to register the container, so edit the token and add **Account → Containers: Edit**. Without it, the deploy fails at `/accounts/…/containers/me`.
2. **Deploy commands.** The production deploy command should be `npx wrangler deploy`, with the Root directory left as the repo root. For non-production branches, set the command to `npx wrangler versions upload` or turn those builds off. Otherwise every pushed branch deploys straight to production.
3. **Worker name.** The name in `wrangler.jsonc` (`polyfi-bot`) must match the Worker's name in Cloudflare.

Add the secrets under **Settings → Variables and Secrets**.

## Going live

The deploy starts in **paper** mode, set by `vars.POLYFI_MODE` in `wrangler.jsonc`. Before trading real money, work through this checklist.

**Secrets** (Settings → Variables and Secrets → type **Secret**; never put these in the repo):
* `POLYFI_DASHBOARD_TOKEN`: a long random string.
* Keys, as **one** of these sets:
  * **Owner key:** `SIGNER_PRIVATE_KEY` and `POLYMARKET_RELAYER_API_KEY`. The signer address is already in `wrangler.jsonc` as `POLYMARKET_RELAYER_API_KEY_ADDRESS`.
  * **Session key (safer, because it can't withdraw):** `POLYMARKET_SESSION_PRIVATE_KEY`.

**Variables** (edit `wrangler.jsonc` and commit). Values set in the dashboard are overwritten by `wrangler.jsonc` on the next deploy:
* `POLYMARKET_WALLET_ADDRESS`: your Polymarket account wallet, from the profile menu on polymarket.com. It's required for session keys and for legacy Safe/Proxy accounts. Without it, the bot uses the signer's own Deposit Wallet.
* `POLYFI_WITHDRAW_ADDRESS`: the **only** address withdrawals may go to, for example your Coinbase USDC (Polygon) address. On a public dashboard, withdrawals stay disabled until this is set, so a leaked token can't send funds anywhere else.
* `POLYFI_AUTOSTART`: set to `"true"` to resume trading automatically after a container restart or redeploy. By default a restart comes back **paused**.
* `POLYFI_MODE`: set to `"live"` last.

**Safeguards built in:**
* **Geoblock check.** On a live start the bot asks Polymarket's geoblock endpoint whether trading is allowed from the server's location. If it isn't, the bot does **not** go live; it runs in paper mode and shows the reason on the dashboard. The same happens for any other live startup failure, such as a bad key, so the container never crash-loops.
* **Daily loss limit.** In live mode the limit also checks Polymarket's own realized PnL since UTC midnight, so a restart, which wipes the local journal, doesn't reset it.

## Know before you go

* **The dashboard is public.** Only the token protects it, so make it long. For a second lock, put the URL behind **Cloudflare Access** (Zero Trust → Access → Applications). It's free for up to 50 users.
* **Container disk is temporary.** The trade journal and recorded ticks (`/data/polyfi.sqlite3`) are wiped when the container restarts or redeploys. Your money and positions are safe, because they live on Polymarket, not in the container.
* **Location.** Cloudflare decides where the container runs. Polymarket blocks some regions, so live orders can be rejected depending on that location. Its terms apply to you as the user, wherever the server runs. The bot does not route around geoblocks.
* **"A request to the Cloudflare API (/accounts/…/containers/me) failed"** at the end of a deploy means the image built and the Worker uploaded, but the container couldn't be registered. Either the account isn't on Workers Paid, or the build's API token lacks **Containers: Edit** (see Option C). Fix whichever applies, then retry the build.
* **Logs.** Go to Workers & Pages → Containers → polyfi-bot in the dashboard, or run `npx wrangler tail`.
