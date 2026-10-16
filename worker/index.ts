// Cloudflare Worker in front of one always-on PolyFi container.
// Uses the built-in Durable Object container API (no npm runtime dependencies).
import { DurableObject } from "cloudflare:workers";

interface Env {
  POLYFI: DurableObjectNamespace<PolyFi>;
  [key: string]: unknown;
}

const PORT = 8787;
const INACTIVITY_TIMEOUT_MS = 6 * 60 * 60 * 1000; // max allowed; the cron also pings every minute

// Settings (wrangler.jsonc "vars") and secrets (`wrangler secret put …`) handed to the bot.
const PASSTHROUGH = [
  "POLYFI_MODE",
  "POLYFI_WINDOW_SECONDS",
  "POLYFI_FREE_ONLY",
  "POLYFI_STAKE_USD",
  "POLYFI_MAX_DAILY_LOSS_USD",
  "POLYFI_DASHBOARD_TOKEN",
  "SIGNER_PRIVATE_KEY",
  "POLYMARKET_WALLET_ADDRESS",
  "POLYMARKET_RELAYER_API_KEY",
  "POLYMARKET_RELAYER_API_KEY_ADDRESS",
  "POLYMARKET_SESSION_PRIVATE_KEY",
];

export class PolyFi extends DurableObject<Env> {
  // True once /healthz has answered for the current container run; steady-state
  // requests then go straight through without an extra health check.
  private ready = false;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const container = ctx.container;
    if (container?.running) {
      void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS));
    }
  }

  private async ensureRunning(): Promise<Fetcher> {
    const container = this.ctx.container;
    if (!container) throw new Error("No container is configured for this Durable Object");
    if (!container.running) {
      this.ready = false;
      const env: Record<string, string> = {
        POLYFI_HOST: "0.0.0.0",
        POLYFI_PORT: String(PORT),
        POLYFI_DB_PATH: "/data/polyfi.sqlite3",
      };
      for (const key of PASSTHROUGH) {
        const value = this.env[key];
        if (typeof value === "string" && value) env[key] = value;
      }
      container.start({ env, enableInternet: true });
      this.ctx.waitUntil(
        container
          .monitor()
          .catch((err) => console.error("polyfi container stopped", err))
          .finally(() => {
            this.ready = false;
          }),
      );
      await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
    }
    const port = container.getTcpPort(PORT);
    if (this.ready) return port;
    // start() returns before the server listens; wait for /healthz (up to ~60s).
    for (let attempt = 0; attempt < 60; attempt++) {
      try {
        const res = await port.fetch("http://container/healthz");
        if (res.ok) {
          this.ready = true;
          return port;
        }
      } catch {
        // not listening yet
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error("PolyFi container did not become ready");
  }

  async fetch(request: Request): Promise<Response> {
    try {
      const port = await this.ensureRunning();
      try {
        return await port.fetch(request);
      } catch (err) {
        this.ready = false; // re-check health on the next request
        throw err;
      }
    } catch (err) {
      return new Response(`PolyFi is starting or unavailable: ${err}`, { status: 503 });
    }
  }
}

const bot = (env: Env) => env.POLYFI.getByName("main");

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!env.POLYFI_DASHBOARD_TOKEN) {
      return new Response("Set the POLYFI_DASHBOARD_TOKEN secret first.", { status: 503 });
    }
    return bot(env).fetch(request);
  },

  // Keep-alive: runs every minute so the bot never sleeps.
  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    if (env.POLYFI_DASHBOARD_TOKEN) {
      ctx.waitUntil(bot(env).fetch(new Request("http://polyfi/healthz")));
    }
  },
} satisfies ExportedHandler<Env>;
