"""
Async SQLite DB layer — aiosqlite + WAL mode.

Implements the SRS Section 6.1 schema.
All I/O is asynchronous to avoid blocking the event loop.
Production: use aiosqlite.
Fallback: sqlite3 + asyncio.to_thread (when aiosqlite is unavailable).
"""
from __future__ import annotations
import asyncio
import json
import sqlite3
from typing import Any

try:
    import aiosqlite
    _HAS_AIOSQLITE = True
except ImportError:
    _HAS_AIOSQLITE = False

from utils.logger import log
from utils.time_utils import utc_now_iso
import config as cfg

# ──────────────────────────── Schema DDL ────────────────────────────

_SCHEMA_SQL = """
-- Strategy state (for restoration after server restart)
CREATE TABLE IF NOT EXISTS strategy_state (
    strategy_id     TEXT PRIMARY KEY,
    config_json     TEXT NOT NULL,
    account_balance REAL NOT NULL,
    total_pnl       REAL DEFAULT 0,
    total_fees      REAL DEFAULT 0,
    total_funding   REAL DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    liquidations    INTEGER DEFAULT 0,
    peak_balance    REAL NOT NULL,
    max_drawdown_pct REAL DEFAULT 0,
    gross_wins_sum  REAL DEFAULT 0,
    gross_losses_sum REAL DEFAULT 0,
    is_active       INTEGER DEFAULT 1,
    updated_at      TEXT NOT NULL
);

-- Active positions
CREATE TABLE IF NOT EXISTS positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id         TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    entry_price         REAL NOT NULL,
    actual_entry        REAL NOT NULL,
    entry_time          TEXT NOT NULL,
    quantity            REAL NOT NULL,
    leverage            INTEGER DEFAULT 1,
    stop_loss           REAL,
    take_profit         REAL,
    trailing_stop       REAL,
    trailing_activated  INTEGER DEFAULT 0,
    highest_price       REAL,
    lowest_price        REAL,
    candle_count        INTEGER DEFAULT 0,
    time_limit          INTEGER,
    liquidation_price   REAL,
    margin_used         REAL,
    notional            REAL,
    entry_fee           REAL NOT NULL,
    cumulative_funding  REAL DEFAULT 0,
    unrealized_pnl      REAL DEFAULT 0,
    trail_atr_mult      REAL DEFAULT 0,
    sl_atr_mult         REAL DEFAULT 0,
    entry_atr           REAL DEFAULT 0,
    status              TEXT DEFAULT 'OPEN',
    FOREIGN KEY (strategy_id) REFERENCES strategy_state(strategy_id)
);

-- Trade records (completed exits)
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL NOT NULL,
    entry_time      TEXT NOT NULL,
    exit_time       TEXT NOT NULL,
    quantity        REAL NOT NULL,
    leverage        INTEGER DEFAULT 1,
    exit_reason     TEXT NOT NULL,
    gross_pnl       REAL NOT NULL,
    total_fees      REAL NOT NULL,
    funding_paid    REAL DEFAULT 0,
    net_pnl         REAL NOT NULL,
    net_pnl_pct     REAL NOT NULL,
    balance_after   REAL NOT NULL,
    candles_held    INTEGER,
    actual_entry    REAL DEFAULT 0,
    actual_exit     REAL DEFAULT 0,
    FOREIGN KEY (strategy_id) REFERENCES strategy_state(strategy_id)
);

-- Equity-curve snapshots
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    balance         REAL NOT NULL,
    unrealized_pnl  REAL DEFAULT 0,
    total_equity    REAL NOT NULL,
    drawdown_pct    REAL DEFAULT 0,
    FOREIGN KEY (strategy_id) REFERENCES strategy_state(strategy_id)
);

-- Candle buffer (for indicator calculation)
CREATE TABLE IF NOT EXISTS candle_buffer (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    open_time   INTEGER NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    UNIQUE(symbol, timeframe, open_time)
);

-- System log
CREATE TABLE IF NOT EXISTS system_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    level       TEXT NOT NULL,
    module      TEXT NOT NULL,
    message     TEXT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_trades_strategy
    ON trades(strategy_id, exit_time);
CREATE INDEX IF NOT EXISTS idx_equity_strategy
    ON equity_snapshots(strategy_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_candle_lookup
    ON candle_buffer(symbol, timeframe, open_time);
CREATE INDEX IF NOT EXISTS idx_positions_active
    ON positions(strategy_id, status);
"""


class Database:
    """Async SQLite database."""

    def __init__(self, path: str = cfg.DB_PATH):
        self._path = path
        self._db = None  # aiosqlite.Connection or sqlite3.Connection
        self._use_aiosqlite = _HAS_AIOSQLITE

    # ────── Connection management ──────

    async def connect(self):
        if self._use_aiosqlite:
            self._db = await aiosqlite.connect(self._path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._init_schema_async()
        else:
            self._db = sqlite3.connect(self._path)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA_SQL)
            self._db.commit()
            # Existing DB migration (sync)
            for sql in [
                "ALTER TABLE strategy_state ADD COLUMN gross_wins_sum REAL DEFAULT 0",
                "ALTER TABLE strategy_state ADD COLUMN gross_losses_sum REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN actual_entry REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN actual_exit REAL DEFAULT 0",
            ]:
                try:
                    self._db.execute(sql)
                except Exception:
                    pass
            self._db.commit()
        log.info("DB 연결 완료: %s (WAL, driver=%s)",
                 self._path, "aiosqlite" if self._use_aiosqlite else "sqlite3")

    async def close(self):
        if self._db:
            if self._use_aiosqlite:
                await self._db.close()
            else:
                self._db.close()
            self._db = None
            log.info("DB 연결 종료")

    async def _init_schema_async(self):
        assert self._db
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        await self._migrate()

    async def _migrate(self):
        """Add new columns to an existing DB (ignore if already present)."""
        migrations = [
            "ALTER TABLE strategy_state ADD COLUMN gross_wins_sum REAL DEFAULT 0",
            "ALTER TABLE strategy_state ADD COLUMN gross_losses_sum REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN actual_entry REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN actual_exit REAL DEFAULT 0",
        ]
        for sql in migrations:
            try:
                await self._db.execute(sql)
            except Exception:
                pass  # Column already exists
        await self._db.commit()

    # ────── Generic helpers ──────

    async def execute(self, sql: str, params: tuple | list = ()) -> int:
        assert self._db
        if self._use_aiosqlite:
            cursor = await self._db.execute(sql, params)
            await self._db.commit()
            return cursor.lastrowid or 0
        else:
            cursor = self._db.execute(sql, params)
            self._db.commit()
            return cursor.lastrowid or 0

    async def execute_many(self, sql: str, params_list: list[tuple]):
        assert self._db
        if self._use_aiosqlite:
            await self._db.executemany(sql, params_list)
            await self._db.commit()
        else:
            self._db.executemany(sql, params_list)
            self._db.commit()

    async def fetch_one(self, sql: str, params: tuple | list = ()) -> dict | None:
        assert self._db
        if self._use_aiosqlite:
            cursor = await self._db.execute(sql, params)
            row = await cursor.fetchone()
        else:
            cursor = self._db.execute(sql, params)
            row = cursor.fetchone()
        return dict(row) if row else None

    async def fetch_all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        assert self._db
        if self._use_aiosqlite:
            cursor = await self._db.execute(sql, params)
            rows = await cursor.fetchall()
        else:
            cursor = self._db.execute(sql, params)
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    # ══════════════════════ strategy_state ══════════════════════

    async def upsert_strategy_state(
        self, strategy_id: str, config_json: str,
        balance: float, peak_balance: float,
        total_pnl: float = 0, total_fees: float = 0,
        total_funding: float = 0, wins: int = 0, losses: int = 0,
        liquidations: int = 0, max_dd: float = 0,
        gross_wins_sum: float = 0, gross_losses_sum: float = 0,
        is_active: bool = True,
    ):
        await self.execute(
            """INSERT INTO strategy_state
               (strategy_id, config_json, account_balance, peak_balance,
                total_pnl, total_fees, total_funding, wins, losses,
                liquidations, max_drawdown_pct,
                gross_wins_sum, gross_losses_sum,
                is_active, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(strategy_id) DO UPDATE SET
                 account_balance=excluded.account_balance,
                 peak_balance=excluded.peak_balance,
                 total_pnl=excluded.total_pnl,
                 total_fees=excluded.total_fees,
                 total_funding=excluded.total_funding,
                 wins=excluded.wins, losses=excluded.losses,
                 liquidations=excluded.liquidations,
                 max_drawdown_pct=excluded.max_drawdown_pct,
                 gross_wins_sum=excluded.gross_wins_sum,
                 gross_losses_sum=excluded.gross_losses_sum,
                 is_active=excluded.is_active,
                 updated_at=excluded.updated_at""",
            (strategy_id, config_json, balance, peak_balance,
             total_pnl, total_fees, total_funding, wins, losses,
             liquidations, max_dd, gross_wins_sum, gross_losses_sum,
             int(is_active), utc_now_iso()),
        )

    async def load_all_strategy_states(self) -> list[dict]:
        return await self.fetch_all("SELECT * FROM strategy_state")

    # ══════════════════════ positions ══════════════════════

    async def insert_position(self, pos) -> int:
        """Insert a Position object into the DB. Returns: row id."""
        return await self.execute(
            """INSERT INTO positions
               (strategy_id, symbol, side, entry_price, actual_entry,
                entry_time, quantity, leverage, stop_loss, take_profit,
                trailing_stop, trailing_activated, highest_price, lowest_price,
                candle_count, time_limit, liquidation_price, margin_used,
                notional, entry_fee, cumulative_funding, unrealized_pnl,
                trail_atr_mult, sl_atr_mult, entry_atr, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos.strategy_id, pos.symbol, pos.side, pos.entry_price,
             pos.actual_entry, pos.entry_time, pos.quantity, pos.leverage,
             pos.stop_loss, pos.take_profit, pos.trailing_stop,
             int(pos.trailing_activated), pos.highest_price, pos.lowest_price,
             pos.candle_count, pos.time_limit, pos.liquidation_price,
             pos.margin_used, pos.notional, pos.entry_fee,
             pos.cumulative_funding, pos.unrealized_pnl,
             pos.trail_atr_mult, pos.sl_atr_mult, pos.entry_atr, "OPEN"),
        )

    async def update_position(self, pos):
        """Update position state (extremes, trailing, funding, etc.)."""
        await self.execute(
            """UPDATE positions SET
                 trailing_stop=?, trailing_activated=?,
                 highest_price=?, lowest_price=?,
                 candle_count=?, cumulative_funding=?,
                 unrealized_pnl=?, status=?
               WHERE strategy_id=? AND symbol=? AND status='OPEN'""",
            (pos.trailing_stop, int(pos.trailing_activated),
             pos.highest_price, pos.lowest_price,
             pos.candle_count, pos.cumulative_funding,
             pos.unrealized_pnl, "OPEN",
             pos.strategy_id, pos.symbol),
        )

    async def close_position(self, strategy_id: str, symbol: str):
        """Mark the position as CLOSED."""
        await self.execute(
            "UPDATE positions SET status='CLOSED' WHERE strategy_id=? AND symbol=? AND status='OPEN'",
            (strategy_id, symbol),
        )

    async def load_open_positions(self) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM positions WHERE status='OPEN'"
        )

    # ══════════════════════ trades ══════════════════════

    async def insert_trade(self, trade) -> int:
        return await self.execute(
            """INSERT INTO trades
               (strategy_id, symbol, side, entry_price, exit_price,
                entry_time, exit_time, quantity, leverage, exit_reason,
                gross_pnl, total_fees, funding_paid, net_pnl, net_pnl_pct,
                balance_after, candles_held, actual_entry, actual_exit)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade.strategy_id, trade.symbol, trade.side,
             trade.entry_price, trade.exit_price,
             trade.entry_time, trade.exit_time,
             trade.quantity, trade.leverage, trade.exit_reason,
             trade.gross_pnl, trade.total_fees, trade.funding_paid,
             trade.net_pnl, trade.net_pnl_pct,
             trade.balance_after, trade.candles_held,
             trade.actual_entry, trade.actual_exit),
        )

    async def get_recent_trades(
        self, strategy_id: str, limit: int = 10
    ) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM trades WHERE strategy_id=? ORDER BY exit_time DESC LIMIT ?",
            (strategy_id, limit),
        )

    async def get_all_trades(self, strategy_id: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM trades WHERE strategy_id=? ORDER BY exit_time",
            (strategy_id,),
        )

    async def get_today_trades(self, date_str: str) -> list[dict]:
        """date_str = '2026-03-29'."""
        return await self.fetch_all(
            "SELECT * FROM trades WHERE exit_time >= ? ORDER BY exit_time",
            (date_str,),
        )

    # ══════════════════════ equity_snapshots ══════════════════════

    async def insert_equity_snapshot(
        self, strategy_id: str, ts: str,
        balance: float, unrealized: float, equity: float, dd_pct: float,
    ):
        await self.execute(
            """INSERT INTO equity_snapshots
               (strategy_id, timestamp, balance, unrealized_pnl,
                total_equity, drawdown_pct)
               VALUES (?,?,?,?,?,?)""",
            (strategy_id, ts, balance, unrealized, equity, dd_pct),
        )

    async def get_equity_curve(
        self, strategy_id: str, limit: int = 500
    ) -> list[dict]:
        return await self.fetch_all(
            """SELECT * FROM equity_snapshots
               WHERE strategy_id=? ORDER BY timestamp DESC LIMIT ?""",
            (strategy_id, limit),
        )

    # ══════════════════════ candle_buffer ══════════════════════

    async def upsert_candle(
        self, symbol: str, tf: str,
        open_time: int, o: float, h: float, l: float, c: float, v: float,
    ):
        await self.execute(
            """INSERT INTO candle_buffer
               (symbol, timeframe, open_time, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, timeframe, open_time) DO UPDATE SET
                 high=MAX(candle_buffer.high, excluded.high),
                 low=MIN(candle_buffer.low, excluded.low),
                 close=excluded.close, volume=excluded.volume""",
            (symbol, tf, open_time, o, h, l, c, v),
        )

    async def upsert_candles_batch(self, candles: list[tuple]):
        """[(symbol, tf, open_time, o, h, l, c, v), ...]."""
        await self.execute_many(
            """INSERT INTO candle_buffer
               (symbol, timeframe, open_time, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, timeframe, open_time) DO UPDATE SET
                 high=MAX(candle_buffer.high, excluded.high),
                 low=MIN(candle_buffer.low, excluded.low),
                 close=excluded.close, volume=excluded.volume""",
            candles,
        )

    async def load_candles(
        self, symbol: str, tf: str, limit: int = 500
    ) -> list[dict]:
        return await self.fetch_all(
            """SELECT * FROM candle_buffer
               WHERE symbol=? AND timeframe=?
               ORDER BY open_time DESC LIMIT ?""",
            (symbol, tf, limit),
        )

    async def get_last_candle_time(self, symbol: str, tf: str) -> int | None:
        row = await self.fetch_one(
            """SELECT MAX(open_time) as last_time FROM candle_buffer
               WHERE symbol=? AND timeframe=?""",
            (symbol, tf),
        )
        return row["last_time"] if row and row["last_time"] else None

    async def prune_candles(self, symbol: str, tf: str, keep: int = 500):
        """Prune old candles."""
        await self.execute(
            """DELETE FROM candle_buffer
               WHERE symbol=? AND timeframe=? AND open_time NOT IN (
                 SELECT open_time FROM candle_buffer
                 WHERE symbol=? AND timeframe=?
                 ORDER BY open_time DESC LIMIT ?
               )""",
            (symbol, tf, symbol, tf, keep),
        )

    # ══════════════════════ system_log ══════════════════════

    async def insert_log(self, level: str, module: str, message: str):
        await self.execute(
            "INSERT INTO system_log (timestamp, level, module, message) VALUES (?,?,?,?)",
            (utc_now_iso(), level, module, message),
        )

    async def prune_logs(self, days: int = 7):
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await self.execute(
            "DELETE FROM system_log WHERE timestamp < ?", (cutoff,)
        )
