"""The trading loop: one decision per tick, at most one position per window."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .config import Settings
from .feeds import PriceFeed
from .gateways import Book, Gateway, WindowMarket
from .model import decide, prob_up, sigma_per_second, taker_fee, window_slug, window_start
from .store import Store

log = logging.getLogger("polyfi.engine")


@dataclass
class Position:
    trade_id: int
    side: str
    asset: str
    price: float
    size: float
    order_id: str | None = None
    filled: float = 0.0
    fee: float = 0.0
    resting: bool = True


@dataclass
class WindowState:
    market: WindowMarket
    open_price: float
    position: Position | None = None
    attempts: int = 0  # orders placed this window; unfilled cancels allow a retry

MAX_ATTEMPTS_PER_WINDOW = 3


@dataclass
class Snapshot:
    ts: float = 0.0
    btc: float = 0.0
    open_price: float = 0.0
    p_up: float = 0.5
    sigma: float = 0.0
    seconds_left: float = 0.0
    slug: str = ""
    question: str = ""
    up: dict = field(default_factory=dict)
    down: dict = field(default_factory=dict)
    signal: dict = field(default_factory=dict)
    balance: float = 0.0


class Engine:
    def __init__(self, settings: Settings, feed: PriceFeed, gateway: Gateway, store: Store) -> None:
        self.settings, self.feed, self.gw, self.store = settings, feed, gateway, store
        self.running = False  # trading enabled (the loop always runs for the dashboard)
        self.window: WindowState | None = None
        self.snapshot = Snapshot()
        self.prices: deque[tuple[float, float]] = deque(maxlen=900)
        self.events: deque[dict] = deque(maxlen=200)
        self._sigma = 0.0
        self._sigma_at = 0.0
        self._task: asyncio.Task | None = None
        self._lookup_at = 0.0
        self.halted_reason = ""

    # ---- lifecycle -------------------------------------------------------
    def start_loop(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop_loop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._cancel_resting("shutdown")

    def note(self, msg: str, level: str = "info") -> None:
        self.events.appendleft({"ts": time.time(), "level": level, "msg": msg})
        log.log({"warn": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO), msg)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # keep the loop alive; surface the error
                self.note(f"tick error: {err}", "error")
            await asyncio.sleep(self.settings.tick_seconds)

    # ---- one step --------------------------------------------------------
    async def tick(self, now: float | None = None) -> None:
        s = self.settings
        now = time.time() if now is None else now
        start = window_start(now, s.window_seconds)
        end = start + s.window_seconds

        if self.window is not None and self.window.market.start != start:
            await self._finish_window()
        if self.window is None and now - self._lookup_at >= 10:
            self._lookup_at = now
            await self._open_window(start, end)

        btc = await self.feed.spot()
        self.prices.append((now, btc))
        if now - self._sigma_at > 60 or not self._sigma:
            closes = await self.feed.minute_closes(s.vol_lookback_min)
            self._sigma = sigma_per_second(closes) or self._sigma
            self._sigma_at = now

        w = self.window
        seconds_left = end - now
        snap = Snapshot(ts=now, btc=btc, sigma=self._sigma, seconds_left=seconds_left)
        snap.balance = await self.gw.balance()
        if w is None:
            snap.signal = {"action": "waiting", "reason": "no Polymarket market for this window"}
            self.snapshot = snap
            return

        p_up = prob_up(btc, w.open_price, self._sigma, seconds_left)
        up, down = await self.gw.book(w.market.up_asset), await self.gw.book(w.market.down_asset)
        snap.open_price, snap.p_up, snap.slug, snap.question = w.open_price, p_up, w.market.slug, w.market.question
        snap.up, snap.down = _book_dict(up), _book_dict(down)

        await self._manage_position(p_up, up, down, seconds_left)
        snap.signal = await self._maybe_enter(p_up, up, down, now - start, seconds_left)
        self.snapshot = snap

    async def _open_window(self, start: int, end: int) -> None:
        slug = window_slug(self.settings.asset, start, self.settings.window_seconds)
        market = await self.gw.find_market(slug, start, end)
        if market is None:
            self.window = None
            self.note(f"{slug} not listed yet — retrying", "warn")
            return
        self._lookup_at = 0.0
        open_price = await self.feed.open_at(start) or await self.feed.spot()
        self.window = WindowState(market, open_price)
        self.note(f"new window {slug} — price to beat ${open_price:,.2f}")

    async def _maybe_enter(self, p_up: float, up: Book, down: Book, elapsed: float, left: float) -> dict:
        s, w = self.settings, self.window
        d = decide(p_up, up.bid, up.ask, down.bid, down.ask, min_edge=s.min_edge, free_only=s.free_only, tick=w.market.tick)
        signal = {"action": "hold", "side": d.side, "price": d.price, "edge": d.edge, "reason": d.reason}

        blocker = None
        if not self.running:
            blocker = "bot paused"
        elif self.halted_reason:
            blocker = self.halted_reason
        elif w.position is not None:
            blocker = "position open this window"
        elif w.attempts >= MAX_ATTEMPTS_PER_WINDOW:
            blocker = "max attempts this window"
        elif elapsed < s.entry_after_s:
            blocker = f"warming up ({int(s.entry_after_s - elapsed)}s)"
        elif left < s.stop_before_end_s:
            blocker = "too close to window end"
        elif d.side is None:
            blocker = d.reason
        if blocker:
            signal["reason"] = blocker
            return signal

        stats = self.store.stats(self.gw.name)
        if stats["today_pnl"] <= -s.max_daily_loss_usd:
            self.halted_reason = f"daily loss limit hit (${stats['today_pnl']:.2f})"
            self.note(self.halted_reason, "warn")
            signal["reason"] = self.halted_reason
            return signal

        asset = w.market.up_asset if d.side == "UP" else w.market.down_asset
        size = max(w.market.min_size, round(s.stake_usd / d.price, 2))
        if size * d.price > await self.gw.balance():
            signal["reason"] = "insufficient balance"
            return signal

        w.attempts += 1
        try:
            if s.free_only:
                oid = await self.gw.buy_maker(asset, d.price, size)
                tid = self.store.add(mode=self.gw.name, slug=w.market.slug, window_start=w.market.start, side=d.side,
                                     kind="maker", order_id=oid, price=d.price, shares=0, status="resting")
                w.position = Position(tid, d.side, asset, d.price, size, oid)
                self.note(f"posted maker BUY {d.side} {size} @ {d.price:.2f} (fair {p_up if d.side == 'UP' else 1 - p_up:.2f}, fee $0)")
            else:
                shares, avg = await self.gw.buy_taker(asset, s.stake_usd, d.price)
                if shares <= 0:
                    signal["reason"] = "taker order not filled"
                    return signal
                fee = taker_fee(shares, avg)
                tid = self.store.add(mode=self.gw.name, slug=w.market.slug, window_start=w.market.start, side=d.side,
                                     kind="taker", price=avg, shares=shares, cost=shares * avg + fee, fee=fee, status="filled")
                w.position = Position(tid, d.side, asset, avg, shares, filled=shares, fee=fee, resting=False)
                self.note(f"took BUY {d.side} {shares} @ {avg:.2f} (fee ${fee:.3f})")
        except Exception as err:
            self.note(f"order failed: {err}", "error")
            signal["reason"] = "order failed"
            return signal
        signal["action"] = "entered"
        return signal

    async def _manage_position(self, p_up: float, up: Book, down: Book, left: float) -> None:
        w = self.window
        pos = w.position if w else None
        if pos is None or not pos.resting:
            return
        filled = await self.gw.filled(pos.order_id)
        if filled > pos.filled:
            pos.filled = filled
            self.store.update(pos.trade_id, shares=filled, cost=filled * pos.price,
                              status="filled" if filled >= pos.size else "partial")
            self.note(f"filled {filled}/{pos.size} {pos.side} @ {pos.price:.2f}")
        if pos.filled >= pos.size:
            pos.resting = False
            return
        fair = p_up if pos.side == "UP" else 1 - p_up
        if left < self.settings.stop_before_end_s or fair - pos.price < self.settings.min_edge / 2:
            await self._cancel_resting("edge gone" if left >= self.settings.stop_before_end_s else "window closing")

    async def _cancel_resting(self, why: str) -> None:
        pos = self.window.position if self.window else None
        if pos is None or not pos.resting or not pos.order_id:
            return
        await self.gw.cancel(pos.order_id)
        pos.resting = False
        if pos.filled <= 0:
            self.store.update(pos.trade_id, status="cancelled", pnl=0)
            self.window.position = None
        self.note(f"cancelled resting order ({why})")

    async def _finish_window(self) -> None:
        w = self.window
        if w is None:
            return
        await self._cancel_resting("window ended")
        pos = w.position
        if pos and pos.filled > 0:
            close = await self.feed.open_at(w.market.end) or await self.feed.spot()
            up_won = close >= w.open_price
            won = up_won == (pos.side == "UP")
            payout = pos.filled if won else 0.0
            cost = pos.filled * pos.price + pos.fee
            pnl = round(payout - cost, 4)
            self.store.update(pos.trade_id, status="won" if won else "lost", pnl=pnl)
            if self.gw.simulated:
                self.gw.credit(payout)  # type: ignore[attr-defined]
            self.note(f"{w.market.slug}: BTC {'UP' if up_won else 'DOWN'} (${w.open_price:,.2f} → ${close:,.2f}) — "
                      f"{'WON' if won else 'lost'} {pnl:+.2f}", "info")
        if not self.gw.simulated:
            try:
                n = await self.gw.redeem_all()
                if n:
                    self.note(f"redeemed {n} resolved position(s) gaslessly")
            except Exception as err:
                self.note(f"redeem pending: {err}", "warn")
        self.window = None

    # ---- dashboard -------------------------------------------------------
    def state(self) -> dict:
        snap = self.snapshot
        pos = self.window.position if self.window else None
        return {
            "mode": self.gw.name,
            "running": self.running,
            "halted": self.halted_reason,
            "snapshot": snap.__dict__,
            "position": pos.__dict__ if pos else None,
            "prices": [[round(t, 1), round(p, 2)] for t, p in list(self.prices)[-600:]],
            "events": list(self.events)[:40],
            "trades": self.store.recent(30),
            "stats": self.store.stats(self.gw.name),
            "settings": self.settings.public(),
        }


def _book_dict(b: Book) -> dict:
    return {"bid": b.bid, "ask": b.ask}
