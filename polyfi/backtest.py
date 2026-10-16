"""Replay recorded paper/live ticks through the real engine with different settings.

    python -m polyfi.backtest --db polyfi.sqlite3 --min-edge 0.03 --stake 5
    python -m polyfi.backtest --db polyfi.sqlite3 --taker        # compare paying the fee
    python -m polyfi.backtest --db polyfi.sqlite3 --sweep        # grid over min-edge × free/taker

Fills are simulated: a resting maker bid fills only once the recorded ask
reaches it, which ignores queue position, so treat maker results as optimistic.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import math
from dataclasses import replace

from .config import Settings
from .engine import Engine
from .feeds import PriceFeed
from .gateways import Book, WindowMarket, _SimFills
from .store import Store


class Tape:
    """Recorded ticks with a movable clock."""

    def __init__(self, ticks: list[dict]) -> None:
        self.ticks = ticks
        self.ts = [t["ts"] for t in ticks]
        self.now = ticks[0]["ts"] if ticks else 0.0
        self.opens = {t["window_start"]: t["open_price"] for t in ticks}
        self.windows = {t["window_start"]: t for t in ticks}

    def at(self, ts: float) -> dict:
        i = max(0, bisect.bisect_right(self.ts, ts) - 1)
        return self.ticks[i]


class ReplayFeed(PriceFeed):
    name = "replay"

    def __init__(self, tape: Tape) -> None:
        self.tape = tape

    async def spot(self) -> float:
        return self.tape.at(self.tape.now)["btc"]

    async def open_at(self, ts: int) -> float | None:
        if ts in self.tape.opens:  # recorded price to beat (also the previous window's close)
            return self.tape.opens[ts]
        return self.tape.at(ts)["btc"] if ts <= self.tape.now else None

    async def minute_closes(self, n: int) -> list[float]:
        now = self.tape.now
        return [self.tape.at(now - 60 * k)["btc"] for k in range(n, -1, -1) if now - 60 * k >= self.tape.ts[0]]


class ReplayGateway(_SimFills):
    name = "replay"

    def __init__(self, tape: Tape) -> None:
        super().__init__(starting_balance=1_000_000)
        self.tape = tape

    async def find_market(self, slug: str, start: int, end: int) -> WindowMarket | None:
        if start not in self.tape.windows:
            return None
        return WindowMarket(slug, start, end, "up", "down", question=slug)

    async def book(self, asset: str) -> Book:
        t = self.tape.at(self.tape.now)
        return Book(t["up_bid"], t["up_ask"]) if asset == "up" else Book(t["down_bid"], t["down_ask"])


async def run(ticks: list[dict], settings: Settings) -> dict:
    tape = Tape(ticks)
    s = replace(settings, db_path=":memory:", max_daily_loss_usd=math.inf, record_ticks=False)
    engine = Engine(s, ReplayFeed(tape), ReplayGateway(tape), Store(":memory:"))
    engine.running = True
    for t in ticks:
        tape.now = t["ts"]
        await engine.tick(t["ts"])
    if engine.window:  # settle the last window only if we saw its end
        last_end = engine.window.market.end
        if tape.ts[-1] >= last_end - 1:
            tape.now = last_end
            await engine._finish_window()
        else:
            await engine._cancel_resting("tape ended")
    trades = [r for r in engine.store.recent(100_000) if r["status"] in ("won", "lost")]
    pnl = sum(r["pnl"] for r in trades)
    staked = sum(r["cost"] for r in trades)
    return {
        "windows": len(tape.windows),
        "trades": len(trades),
        "win_rate": round(sum(r["status"] == "won" for r in trades) / len(trades), 3) if trades else None,
        "pnl": round(pnl, 2),
        "fees": round(sum(r["fee"] for r in trades), 3),
        "roi": round(pnl / staked, 4) if staked else None,
        "orders": len(engine.store.recent(100_000)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="polyfi.sqlite3")
    ap.add_argument("--since", type=float, default=0, help="unix seconds")
    ap.add_argument("--min-edge", type=float, default=None)
    ap.add_argument("--stake", type=float, default=None)
    ap.add_argument("--taker", action="store_true", help="allow taking the ask (pays fees)")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    base = Settings.from_env()
    if args.min_edge is not None:
        base.min_edge = args.min_edge
    if args.stake is not None:
        base.stake_usd = args.stake
    base.free_only = not args.taker
    ticks = Store(args.db).ticks(args.since)
    if not ticks:
        raise SystemExit("No recorded ticks — run POLYFI_MODE=paper for a while first.")
    print(f"{len(ticks)} ticks from {args.db}")

    if not args.sweep:
        print(asyncio.run(run(ticks, base)))
        return
    print(f"{'mode':6} {'edge':>5} {'trades':>6} {'win%':>6} {'pnl':>9} {'fees':>7} {'roi':>7}")
    for free in (True, False):
        for edge in (0.02, 0.03, 0.04, 0.06, 0.08, 0.10):
            r = asyncio.run(run(ticks, replace(base, free_only=free, min_edge=edge)))
            wr = f"{r['win_rate'] * 100:.0f}" if r["win_rate"] is not None else "-"
            roi = f"{r['roi'] * 100:.1f}%" if r["roi"] is not None else "-"
            print(f"{'maker' if free else 'taker':6} {edge:5.2f} {r['trades']:6} {wr:>6} {r['pnl']:9.2f} {r['fees']:7.3f} {roi:>7}")


if __name__ == "__main__":
    main()
