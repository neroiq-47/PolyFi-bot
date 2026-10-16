"""One-time session-key setup, run on a trusted machine (not the bot server).

A session key can place and cancel orders for your Deposit Wallet but cannot
withdraw funds. Authorizing one needs the wallet OWNER key plus a Builder API key
that Polymarket has enabled for session keys (beta: email builder@polymarket.com).

    python -m polyfi.session_key authorize   # prints a new key for the bot's .env
    python -m polyfi.session_key list
    python -m polyfi.session_key revoke 0xSessionAddress

Owner env: OWNER_PRIVATE_KEY, POLYMARKET_WALLET_ADDRESS,
           POLYMARKET_BUILDER_API_KEY, POLYMARKET_BUILDER_SECRET, POLYMARKET_BUILDER_PASSPHRASE
"""

from __future__ import annotations

import argparse
import asyncio
import os


async def _owner_client():
    from polymarket import AsyncSecureClient, BuilderApiKey

    need = ["OWNER_PRIVATE_KEY", "POLYMARKET_WALLET_ADDRESS", "POLYMARKET_BUILDER_API_KEY",
            "POLYMARKET_BUILDER_SECRET", "POLYMARKET_BUILDER_PASSPHRASE"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")
    return await AsyncSecureClient.create(
        private_key=os.environ["OWNER_PRIVATE_KEY"],
        wallet=os.environ["POLYMARKET_WALLET_ADDRESS"],
        api_key=BuilderApiKey(
            key=os.environ["POLYMARKET_BUILDER_API_KEY"],
            secret=os.environ["POLYMARKET_BUILDER_SECRET"],
            passphrase=os.environ["POLYMARKET_BUILDER_PASSPHRASE"],
        ),
    )


async def authorize() -> None:
    from eth_account import Account
    from polymarket import SessionKeyKnownScope

    key = Account.create()
    async with await _owner_client() as owner:
        # Make sure the wallet's trading approvals exist; a session key cannot set them.
        await owner.setup_trading_approvals()
        await owner.authorize_session_key(address=key.address, scopes=(SessionKeyKnownScope.CLOB,))
    print("Session key authorized for CLOB trading (valid 180 days, cannot withdraw).")
    print("Put these in the BOT's .env — store the private key like a password:\n")
    print(f"POLYMARKET_SESSION_PRIVATE_KEY=0x{key.key.hex().removeprefix('0x')}")
    print(f"POLYMARKET_WALLET_ADDRESS={os.environ['POLYMARKET_WALLET_ADDRESS']}")


async def list_keys() -> None:
    async with await _owner_client() as owner:
        for k in await owner.fetch_session_keys():
            print(k.address, ",".join(map(str, k.scopes)), "valid until", k.valid_until)


async def revoke(address: str) -> None:
    async with await _owner_client() as owner:
        await owner.revoke_session_key(address=address)
    print(f"revoked {address} (its open orders are being cancelled)")


def main() -> None:
    from .config import _load_dotenv
    from pathlib import Path

    _load_dotenv(Path(".env.owner"))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("authorize")
    sub.add_parser("list")
    sub.add_parser("revoke").add_argument("address")
    args = ap.parse_args()
    if args.cmd == "authorize":
        asyncio.run(authorize())
    elif args.cmd == "list":
        asyncio.run(list_keys())
    else:
        asyncio.run(revoke(args.address))


if __name__ == "__main__":
    main()
