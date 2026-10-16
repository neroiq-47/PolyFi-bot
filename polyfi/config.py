"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Polygon contract addresses (from the official polymarket-client SDK / docs).
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
# Native USDC on Polygon — what Coinbase sends/receives on the Polygon network.
USDC_POLYGON = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
POLYGON_CHAIN_ID = "137"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # demo  = fully simulated BTC + order book (works offline, no keys)
    # paper = real BTC price + real Polymarket books, simulated fills (no keys)
    # live  = real orders through Polymarket (needs keys below)
    mode: str = "demo"

    # Market: Polymarket's shortest BTC market is 5 minutes (300s).
    # 15m = 900. If Polymarket ships 1-minute markets, set 60.
    window_seconds: int = 300
    asset: str = "btc"

    # "Free only" = maker-only post-only limit orders: 0 trading fee + maker rebates.
    free_only: bool = True

    # Strategy / risk
    stake_usd: float = 5.0
    min_edge: float = 0.04          # required model edge over price, in probability points
    entry_after_s: int = 20         # wait this long into a window before trading
    stop_before_end_s: int = 60     # stop entering this close to the end
    max_daily_loss_usd: float = 25.0
    vol_lookback_min: int = 60
    tick_seconds: float = 1.0
    record_ticks: bool = True       # paper/live: save ticks for backtesting

    # Live credentials (never sent to the browser)
    signer_private_key: str = field(default="", repr=False)
    wallet_address: str = ""
    relayer_api_key: str = field(default="", repr=False)
    relayer_api_key_address: str = ""
    # Optional: trade with a scoped session key instead (cannot withdraw). See polyfi/session_key.py
    session_private_key: str = field(default="", repr=False)

    # Dashboard
    host: str = "127.0.0.1"
    port: int = 8787
    dashboard_token: str = field(default="", repr=False)
    db_path: str = "polyfi.sqlite3"

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "Settings":
        if env_file:
            _load_dotenv(Path(env_file))
        e = os.environ.get
        return cls(
            mode=e("POLYFI_MODE", "demo").lower(),
            window_seconds=int(e("POLYFI_WINDOW_SECONDS", "300")),
            asset=e("POLYFI_ASSET", "btc").lower(),
            free_only=_bool("POLYFI_FREE_ONLY", True),
            stake_usd=float(e("POLYFI_STAKE_USD", "5")),
            min_edge=float(e("POLYFI_MIN_EDGE", "0.04")),
            entry_after_s=int(e("POLYFI_ENTRY_AFTER_S", "20")),
            stop_before_end_s=int(e("POLYFI_STOP_BEFORE_END_S", "60")),
            max_daily_loss_usd=float(e("POLYFI_MAX_DAILY_LOSS_USD", "25")),
            vol_lookback_min=int(e("POLYFI_VOL_LOOKBACK_MIN", "60")),
            tick_seconds=float(e("POLYFI_TICK_SECONDS", "1")),
            record_ticks=_bool("POLYFI_RECORD_TICKS", True),
            signer_private_key=e("SIGNER_PRIVATE_KEY", ""),
            wallet_address=e("POLYMARKET_WALLET_ADDRESS", ""),
            relayer_api_key=e("POLYMARKET_RELAYER_API_KEY", ""),
            relayer_api_key_address=e("POLYMARKET_RELAYER_API_KEY_ADDRESS", ""),
            session_private_key=e("POLYMARKET_SESSION_PRIVATE_KEY", ""),
            host=e("POLYFI_HOST", "127.0.0.1"),
            port=int(e("POLYFI_PORT", "8787")),
            dashboard_token=e("POLYFI_DASHBOARD_TOKEN", ""),
            db_path=e("POLYFI_DB_PATH", "polyfi.sqlite3"),
        )

    # Fields the dashboard may read and change at runtime.
    EDITABLE = (
        "free_only",
        "stake_usd",
        "min_edge",
        "entry_after_s",
        "stop_before_end_s",
        "max_daily_loss_usd",
    )

    def public(self) -> dict:
        data = asdict(self)
        for secret in ("signer_private_key", "relayer_api_key", "dashboard_token", "session_private_key"):
            data.pop(secret, None)
        data["live_ready"] = self.live_ready
        data["session_key"] = self.uses_session_key
        return data

    @property
    def uses_session_key(self) -> bool:
        return bool(self.session_private_key and self.wallet_address)

    @property
    def live_ready(self) -> bool:
        owner = bool(self.signer_private_key and self.relayer_api_key and self.relayer_api_key_address)
        return owner or self.uses_session_key

    def update(self, changes: dict) -> None:
        for key, value in changes.items():
            if key not in self.EDITABLE:
                raise ValueError(f"{key} cannot be changed from the dashboard")
            current = getattr(self, key)
            if isinstance(current, bool):
                value = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}
            else:
                value = type(current)(value)
                if value < 0:
                    raise ValueError(f"{key} must be >= 0")
            setattr(self, key, value)
        if self.stake_usd < 1:
            raise ValueError("stake_usd must be at least 1 (Polymarket minimum order)")
