"""Deposits and withdrawals through Polymarket's bridge API (Polymarket charges no fee for either).

Withdraw = ask the bridge for a one-time address configured for "USDC on Polygon to <your
Coinbase address>", then send pUSD there with a gasless relayer transfer.
"""

from __future__ import annotations

import httpx

from .config import POLYGON_CHAIN_ID, USDC_POLYGON

BRIDGE = "https://bridge.polymarket.com"


async def deposit_addresses(wallet: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(f"{BRIDGE}/deposit", json={"address": wallet})
        r.raise_for_status()
        return r.json().get("address", {})


async def withdraw_usdc_polygon(gateway, wallet: str, recipient: str, amount_usd: float) -> dict:
    """Withdraw as native USDC on Polygon (e.g. to a Coinbase USDC deposit address, Polygon network)."""
    if amount_usd < 1:
        raise ValueError("minimum withdrawal is $1")
    if not (recipient.startswith("0x") and len(recipient) == 42):
        raise ValueError("recipient must be a 0x… Polygon address")
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(
            f"{BRIDGE}/withdraw",
            json={
                "address": wallet,
                "toChainId": POLYGON_CHAIN_ID,
                "toTokenAddress": USDC_POLYGON,
                "recipientAddr": recipient,
            },
        )
        r.raise_for_status()
        bridge_evm = r.json()["address"]["evm"]
    outcome = await gateway.transfer_pusd(bridge_evm, amount_usd)  # gasless via relayer
    return {"bridge_address": bridge_evm, "amount": amount_usd, "recipient": recipient, "outcome": str(outcome)}
