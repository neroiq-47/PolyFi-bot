import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  POLYFI: DurableObjectNamespace<PolyFi>;
  // Plain vars (wrangler.jsonc "vars")
  POLYFI_MODE: string;
  POLYFI_WINDOW_SECONDS: string;
  POLYFI_FREE_ONLY: string;
  POLYFI_STAKE_USD: string;
  POLYFI_MAX_DAILY_LOSS_USD: string;
  // Secrets (`wrangler secret put …`) — never committed
  POLYFI_DASHBOARD_TOKEN: string;
  SIGNER_PRIVATE_KEY?: string;
  POLYMARKET_WALLET_ADDRESS?: string;
  POLYMARKET_RELAYER_API_KEY?: string;
  POLYMARKET_RELAYER_API_KEY_ADDRESS?: string;
  POLYMARKET_SESSION_PRIVATE_KEY?: string;
}

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
] as const;

// One always-on bot instance. The cron below pings it every minute so it never sleeps.
export class PolyFi extends Container<Env> {
  defaultPort = 8787;
  sleepAfter = "15m";
  enableInternet = true;

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    const vars: Record<string, string> = {
      POLYFI_HOST: "0.0.0.0",
      POLYFI_PORT: "8787",
      POLYFI_DB_PATH: "/data/polyfi.sqlite3",
    };
    for (const key of PASSTHROUGH) {
      const value = env[key];
      if (value) vars[key] = value;
    }
    this.envVars = vars;
  }

  override onError(error: unknown) {
    console.error("polyfi container error", error);
  }
}

const bot = (env: Env) => getContainer(env.POLYFI, "main");

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!env.POLYFI_DASHBOARD_TOKEN) {
      return new Response("Set the POLYFI_DASHBOARD_TOKEN secret first.", { status: 503 });
    }
    return bot(env).fetch(request);
  },

  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(bot(env).fetch(new Request("http://polyfi/healthz")));
  },
} satisfies ExportedHandler<Env>;
