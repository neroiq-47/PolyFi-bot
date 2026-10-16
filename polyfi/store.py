"""SQLite trade journal."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    mode TEXT NOT NULL,
    slug TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    side TEXT NOT NULL,
    kind TEXT NOT NULL,
    order_id TEXT,
    price REAL NOT NULL,
    shares REAL NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    pnl REAL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(self, **row) -> int:
        row.setdefault("created_at", time.time())
        cols = ",".join(row)
        cur = self.db.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        self.db.commit()
        return int(cur.lastrowid)

    def update(self, trade_id: int, **fields) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE trades SET {sets} WHERE id=?", [*fields.values(), trade_id])
        self.db.commit()

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self, mode: str) -> dict:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        q = "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(status='won'),0) w, COALESCE(SUM(fee),0) fees FROM trades WHERE mode=? AND status IN ('won','lost')"
        all_ = self.db.execute(q, (mode,)).fetchone()
        today = self.db.execute(q + " AND created_at>=?", (mode, day_start)).fetchone()
        return {
            "trades": all_["n"],
            "wins": all_["w"],
            "win_rate": round(all_["w"] / all_["n"], 3) if all_["n"] else None,
            "pnl": round(all_["pnl"], 2),
            "fees": round(all_["fees"], 4),
            "today_trades": today["n"],
            "today_pnl": round(today["pnl"], 2),
        }
