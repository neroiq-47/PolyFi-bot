"""Exchange gateways: demo (all simulated), paper (real books, simulated fills), live (real orders).

All live wallet actions go through Polymarket's relayer, so they are gasless.
"""

from __future__ import annotations

import itertools
import logging
import random
from dataclasses import dataclass, field
from decimal import Decimal

from .config import PUSD_ADDRESS, Settings
from .feeds import PriceFeed
from .model import prob_up

log = logging.getLogger("polyfi.gateway")


@dataclass
class WindowMarket:
    slug: str
    start: int
    end: int
    up_asset: str
    down_asset: str
    condition_id: str = ""
    tick: float = 0.01
    min_size: float = 5.0
    question: str = ""


@dataclass
class Book:
    bid: float | None
    ask: float | None


@dataclass
class SimOrder:
    id: str
    asset: str
    price: float
    size: float
    filled: float = 0.0
    cancelled: bool = False


class Gateway:
    name = "base"
    simulated = True

    async def find_market(self, slug: str, start: int, end: int) -> WindowMarket | None:
        raise NotImplementedError

    async def book(self, asset: str) -> Book:
        raise NotImplementedError

    async def buy_maker(self, asset: str, price: float, size: float) -> str:
        raise NotImplementedError

    async def buy_taker(self, asset: str, usd: float, max_price: float) -> tuple[float, float]:
        """Returns (shares filled, avg price)."""
        raise NotImplementedError

    async def filled(self, order_id: str) -> float:
        raise NotImplementedError

    async def cancel(self, order_id: str) -> None:
        raise NotImplementedError

    async def balance(self) -> float:
        raise NotImplementedError

    async def redeem_all(self) -> int:
        return 0

    async def close(self) -> None:
        pass


class _SimFills(Gateway):
    """Shared simulated balance + maker-fill logic for demo and paper modes."""

    def __init__(self, starting_balance: float = 100.0) -> None:
        self._balance = starting_balance
        self._orders: dict[str, SimOrder] = {}
        self._ids = itertools.count(1)

    async def buy_maker(self, asset: str, price: float, size: float) -> str:
        oid = f"sim-{next(self._ids)}"
        self._orders[oid] = SimOrder(oid, asset, price, size)
        self._balance -= price * size  # reserve
        return oid

    async def filled(self, order_id: str) -> float:
        o = self._orders[order_id]
        if not o.cancelled and o.filled < o.size:
            book = await self.book(o.asset)
            # A resting bid fills when sellers reach our price.
            if book.ask is not None and book.ask <= o.price:
                o.filled = o.size
        return o.filled

    async def cancel(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        if o and not o.cancelled:
            o.cancelled = True
            self._balance += o.price * (o.size - o.filled)  # release unfilled reserve

    async def buy_taker(self, asset: str, usd: float, max_price: float) -> tuple[float, float]:
        book = await self.book(asset)
        if book.ask is None or book.ask > max_price:
            return 0.0, 0.0
        from .model import taker_fee

        shares = round(usd / book.ask, 2)
        self._balance -= shares * book.ask + taker_fee(shares, book.ask)
        return shares, book.ask

    def credit(self, amount: float) -> None:
        self._balance += amount

    async def balance(self) -> float:
        return round(self._balance, 2)


class DemoGateway(_SimFills):
    """Synthetic order book: a slightly lagged, noisy copy of fair value."""

    name = "demo"

    def __init__(self, feed: PriceFeed, settings: Settings, seed: int | None = None) -> None:
        super().__init__()
        self.feed, self.settings = feed, settings
        self._rng = random.Random(seed)
        self._market: WindowMarket | None = None
        self._open: float | None = None
        self._lagged: float = 0.5

    async def find_market(self, slug: str, start: int, end: int) -> WindowMarket:
        self._market = WindowMarket(slug, start, end, f"{slug}:up", f"{slug}:down", question=f"Bitcoin Up or Down ({slug})")
        self._open = await self.feed.open_at(start)
        self._lagged = 0.5
        return self._market

    async def book(self, asset: str) -> Book:
        import time

        m = self._market
        if m is None or self._open is None:
            return Book(None, None)
        sigma = getattr(self.feed, "sigma", 0.00006)
        p = prob_up(await self.feed.spot(), self._open, sigma, m.end - time.time())
        self._lagged += 0.35 * (p - self._lagged)
        mid = min(0.98, max(0.02, self._lagged + self._rng.gauss(0, 0.015)))
        if asset.endswith(":down"):
            mid = 1 - mid
        bid = max(0.01, round(mid - 0.01, 2))
        return Book(bid, min(0.99, round(bid + 0.02, 2)))


class PaperGateway(_SimFills):
    """Real Polymarket markets and books via the public API; fills are simulated."""

    name = "paper"

    def __init__(self) -> None:
        super().__init__()
        from polymarket import AsyncPublicClient

        self._client = AsyncPublicClient()

    async def find_market(self, slug: str, start: int, end: int) -> WindowMarket | None:
        return await _find_market(self._client, slug, start, end)

    async def book(self, asset: str) -> Book:
        return await _book(self._client, asset)

    async def close(self) -> None:
        await self._client.close()


class LiveGateway(Gateway):
    """Real trading through the official ``polymarket-client`` SDK (Deposit Wallet + relayer = gasless)."""

    name = "live"
    simulated = False

    def __init__(self, client) -> None:  # AsyncSecureClient
        self._client = client

    @classmethod
    async def connect(cls, settings: Settings) -> "LiveGateway":
        from polymarket import AsyncSecureClient, RelayerApiKey

        if not settings.live_ready:
            raise RuntimeError("Live mode needs SIGNER_PRIVATE_KEY, POLYMARKET_RELAYER_API_KEY and _ADDRESS")
        client = await AsyncSecureClient.create(
            private_key=settings.signer_private_key,
            wallet=settings.wallet_address or None,
            api_key=RelayerApiKey(key=settings.relayer_api_key, address=settings.relayer_api_key_address),
        )
        # Idempotent; gasless via relayer. Lets the exchange spend pUSD / outcome tokens.
        await client.setup_trading_approvals()
        return cls(client)

    @property
    def wallet(self) -> str:
        return str(self._client.wallet)

    async def find_market(self, slug: str, start: int, end: int) -> WindowMarket | None:
        return await _find_market(self._client, slug, start, end)

    async def book(self, asset: str) -> Book:
        return await _book(self._client, asset)

    async def buy_maker(self, asset: str, price: float, size: float) -> str:
        resp = await self._client.place_limit_order(
            asset_id=asset, price=Decimal(str(price)), size=Decimal(str(size)), side="BUY", post_only=True
        )
        if not resp.ok:
            raise RuntimeError(f"order rejected: {resp.code} {resp.message}")
        return str(resp.order_id)

    async def buy_taker(self, asset: str, usd: float, max_price: float) -> tuple[float, float]:
        resp = await self._client.place_market_order(
            asset_id=asset, side="BUY", amount=Decimal(str(usd)), max_price=Decimal(str(max_price)), order_type="FAK"
        )
        if not resp.ok:
            raise RuntimeError(f"order rejected: {resp.code} {resp.message}")
        shares = float(resp.taking_amount)
        return shares, (float(resp.making_amount) / shares if shares else 0.0)

    async def filled(self, order_id: str) -> float:
        order = await self._client.get_order(order_id=order_id)
        return float(order.size_matched)

    async def cancel(self, order_id: str) -> None:
        await self._client.cancel_order(order_id=order_id)

    async def balance(self) -> float:
        bal = await self._client.get_balance_allowance(asset_type="COLLATERAL")
        return bal.balance / 1e6

    async def redeem_all(self) -> int:
        count = 0
        async for pos in self._client.list_positions(status="REDEEMABLE").iter_items():
            if float(pos.current_size) <= 0:
                continue
            handle = await self._client.redeem_positions(condition_id=str(pos.condition_id))
            await handle.wait()
            count += 1
        return count

    async def transfer_pusd(self, to_address: str, amount_usd: float):
        handle = await self._client.transfer_erc20(
            token_address=PUSD_ADDRESS, recipient_address=to_address, amount=int(round(amount_usd * 1e6))
        )
        return await handle.wait()

    async def close(self) -> None:
        await self._client.close()


async def _find_market(client, slug: str, start: int, end: int) -> WindowMarket | None:
    from polymarket import PolymarketError

    try:
        m = await client.get_market(slug=slug)
    except PolymarketError as err:
        log.info("market %s not found: %s", slug, err)
        return None
    outcomes = {o.label.strip().lower(): o for o in (m.outcomes.yes, m.outcomes.no)}
    up, down = outcomes.get("up"), outcomes.get("down")
    if up is None or down is None:
        up, down = m.outcomes.yes, m.outcomes.no
    asset = lambda o: str(o.token_id or o.position_id or "")  # noqa: E731
    if not asset(up) or not asset(down):
        return None
    return WindowMarket(
        slug=slug,
        start=start,
        end=end,
        up_asset=asset(up),
        down_asset=asset(down),
        condition_id=str(m.condition_id or ""),
        tick=float(m.trading.minimum_tick_size or Decimal("0.01")),
        min_size=float(m.trading.minimum_order_size or Decimal("5")),
        question=m.question or slug,
    )


async def _book(client, asset: str) -> Book:
    ob = await client.get_order_book(asset_id=asset)
    bid = max((float(lv.price) for lv in ob.bids), default=None)
    ask = min((float(lv.price) for lv in ob.asks), default=None)
    return Book(bid, ask)
