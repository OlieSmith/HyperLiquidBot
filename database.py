import sqlite3
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "trades.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                direction TEXT NOT NULL,
                strategy TEXT NOT NULL,
                conviction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                size_usd REAL NOT NULL,
                size_coin REAL NOT NULL,
                leverage INTEGER NOT NULL,
                pnl_usd REAL,
                pnl_pct REAL,
                status TEXT NOT NULL DEFAULT 'open',
                open_time TEXT NOT NULL,
                close_time TEXT,
                close_reason TEXT,
                paper_trade INTEGER NOT NULL DEFAULT 1,
                order_id TEXT
            );

            CREATE TABLE IF NOT EXISTS trailing_stops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                coin TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                trail_pct REAL NOT NULL,
                high_water_mark REAL NOT NULL,
                stop_price REAL NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_coin ON trades(coin);
            CREATE INDEX IF NOT EXISTS idx_trailing_coin ON trailing_stops(coin);
        """)
        conn.commit()
        logger.info("Database initialized")
    finally:
        conn.close()


def open_trade(
    coin: str,
    direction: str,
    strategy: str,
    conviction: str,
    entry_price: float,
    size_usd: float,
    size_coin: float,
    leverage: int,
    paper_trade: bool,
    order_id: Optional[str] = None,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO trades
              (coin, direction, strategy, conviction, entry_price, size_usd, size_coin,
               leverage, status, open_time, paper_trade, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                coin, direction, strategy, conviction, entry_price,
                size_usd, size_coin, leverage,
                datetime.utcnow().isoformat(), int(paper_trade), order_id,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def close_trade(
    trade_id: int,
    exit_price: float,
    close_reason: str,
) -> dict:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not row:
            return {}
        row = dict(row)
        entry = row["entry_price"]
        direction = row["direction"]
        size_coin = row["size_coin"]
        leverage = row["leverage"]

        if direction == "long":
            pnl_usd = (exit_price - entry) * size_coin * leverage
            pnl_pct = ((exit_price - entry) / entry) * 100 * leverage
        else:
            pnl_usd = (entry - exit_price) * size_coin * leverage
            pnl_pct = ((entry - exit_price) / entry) * 100 * leverage

        conn.execute(
            """
            UPDATE trades SET
              exit_price = ?, pnl_usd = ?, pnl_pct = ?,
              status = 'closed', close_time = ?, close_reason = ?
            WHERE id = ?
            """,
            (exit_price, pnl_usd, pnl_pct, datetime.utcnow().isoformat(), close_reason, trade_id),
        )
        conn.execute("DELETE FROM trailing_stops WHERE trade_id = ?", (trade_id,))
        conn.commit()

        row.update({"exit_price": exit_price, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "close_reason": close_reason})
        return row
    finally:
        conn.close()


def upsert_trailing_stop(
    trade_id: int,
    coin: str,
    direction: str,
    entry_price: float,
    trail_pct: float,
    high_water_mark: float,
    stop_price: float,
):
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM trailing_stops WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE trailing_stops
                SET high_water_mark = ?, stop_price = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (high_water_mark, stop_price, datetime.utcnow().isoformat(), trade_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO trailing_stops
                  (trade_id, coin, direction, entry_price, trail_pct, high_water_mark, stop_price, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trade_id, coin, direction, entry_price, trail_pct,
                 high_water_mark, stop_price, datetime.utcnow().isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


def get_open_trades() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_trade(trade_id: int) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_trailing_stops() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT ts.*, t.id as trade_id
            FROM trailing_stops ts
            JOIN trades t ON ts.trade_id = t.id
            WHERE t.status = 'open'
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats() -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) as total,
              SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_count,
              SUM(CASE WHEN status='closed' AND pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
              SUM(CASE WHEN status='closed' AND pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
              SUM(CASE WHEN status='closed' THEN pnl_usd ELSE 0 END) as total_pnl,
              AVG(CASE WHEN status='closed' THEN pnl_pct ELSE NULL END) as avg_pnl_pct
            FROM trades
            """
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()
