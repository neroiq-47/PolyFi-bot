# Deploy to Cloudflare

PolyFi runs on **Cloudflare Containers**. A small Worker (`cloudflare/src/index.ts`) sits in front of one always-on container that runs the bot and dashboard (`Dockerfile`). A cron trigger pings the container every minute so it never sleeps.

## Cost

You need the **Workers Paid plan ($5/mo)**. The default `basic` instance (¼ vCPU, 1 GiB) running 24/7 uses about 720 GiB-hours of memory a month. The plan includes 25 GiB-hours, so expect roughly **$12/mo in total**. `lite` (256 MiB) comes to about $7/mo, but it may be too small for Python plus the Polymarket SDK. Prices come from [developers.cloudflare.com/containers/pricing](https://developers.cloudflare.com/containers/pricing/).

## Option A: from your computer (Docker running)

```bash
cd cloudflare
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

If you connected this repo in the Cloudflare dashboard, go to **Workers & Pages → polyfi-bot → Settings → Build**. Set **Root directory** to `cloudflare` and leave the deploy command as `npx wrangler deploy`. The Worker name in `cloudflare/wrangler.jsonc` (`polyfi-bot`) must match the Worker's name in Cloudflare. Add the secrets under **Settings → Variables and Secrets**.

## Going live

The deploy starts in **paper** mode, set by `vars.POLYFI_MODE` in `cloudflare/wrangler.jsonc`. To trade for real:

* Add the key secrets. The session-key setup is recommended, because a session key can't withdraw.
* Change the mode to `live` and redeploy.

## Know before you go

* **The dashboard is public.** Only the token protects it, so make it long. For a second lock, put the URL behind **Cloudflare Access** (Zero Trust → Access → Applications). It's free for up to 50 users.
* **Container disk is temporary.** The trade journal and recorded ticks (`/data/polyfi.sqlite3`) are wiped when the container restarts or redeploys. Your money and positions are safe, because they live on Polymarket, not in the container.
* **Location.** Cloudflare decides where the container runs. Polymarket blocks some regions, so live orders can be rejected depending on that location. Its terms apply to you as the user, wherever the server runs. The bot does not route around geoblocks.
* **Logs.** Go to Workers & Pages → Containers → polyfi-bot in the dashboard, or run `npx wrangler tail`.
