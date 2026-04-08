"""
database.py — Async SQLite persistence layer for SOLPoker Bot.
Tables: users, lobbies, lobby_players, games, jackpot_balance,
        transaction_log, processed_signatures
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solpoker.db")

_db: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    """Return (or create) a singleton async SQLite connection."""
    global _db
    if _db is None:
        async with _db_lock:
            if _db is None:
                _db = await aiosqlite.connect(DB_PATH)
                _db.row_factory = aiosqlite.Row
                await _db.execute("PRAGMA journal_mode=WAL")
                await _db.execute("PRAGMA foreign_keys=ON")
    return _db


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


# ────────────────────────────────────────────────────────────
# Schema initialisation
# ────────────────────────────────────────────────────────────

async def init_db() -> None:
    db = await get_db()

    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id   INTEGER PRIMARY KEY,
            username      TEXT,
            wallet_address TEXT,
            registered_at TEXT NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS lobbies (
            lobby_id            TEXT PRIMARY KEY,
            chat_id             INTEGER NOT NULL,
            creator_id          INTEGER NOT NULL,
            max_players         INTEGER NOT NULL,
            buyin_usd           REAL NOT NULL,
            buyin_sol           REAL,
            sol_price_at_create REAL,
            status              TEXT NOT NULL DEFAULT 'waiting',
            created_at          TEXT NOT NULL,
            message_id          INTEGER,
            prize_pool_usd      REAL,
            multiplier          REAL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS lobby_players (
            lobby_id    TEXT NOT NULL,
            telegram_id INTEGER NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            joined_at   TEXT NOT NULL,
            PRIMARY KEY (lobby_id, telegram_id)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id     TEXT PRIMARY KEY,
            lobby_id    TEXT NOT NULL,
            chat_id     INTEGER NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            game_state  TEXT,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            message_id  INTEGER
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS jackpot_balance (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            balance_usd REAL NOT NULL DEFAULT 0.0,
            updated_at  TEXT NOT NULL
        )
    """)

    await db.execute("""
        INSERT OR IGNORE INTO jackpot_balance (id, balance_usd, updated_at)
        VALUES (1, 0.0, ?)
    """, (datetime.now(timezone.utc).isoformat(),))

    await db.execute("""
        CREATE TABLE IF NOT EXISTS transaction_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            lobby_id      TEXT,
            telegram_id   INTEGER,
            tx_type       TEXT NOT NULL,
            amount_usd    REAL,
            amount_sol    REAL,
            sol_price     REAL,
            tx_signature  TEXT,
            timestamp     TEXT NOT NULL,
            notes         TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS processed_signatures (
            signature    TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL
        )
    """)

    await db.commit()


# ────────────────────────────────────────────────────────────
# User helpers
# ────────────────────────────────────────────────────────────

async def get_user(telegram_id: int) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def register_user(telegram_id: int, username: str, wallet_address: str) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO users (telegram_id, username, wallet_address, registered_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(telegram_id) DO UPDATE
             SET username = excluded.username,
                 wallet_address = excluded.wallet_address""",
        (telegram_id, username, wallet_address, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


async def update_wallet(telegram_id: int, wallet_address: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE users SET wallet_address = ? WHERE telegram_id = ?",
        (wallet_address, telegram_id),
    )
    await db.commit()


# ────────────────────────────────────────────────────────────
# Lobby helpers
# ────────────────────────────────────────────────────────────

async def create_lobby(
    lobby_id: str,
    chat_id: int,
    creator_id: int,
    max_players: int,
    buyin_usd: float,
    buyin_sol: float,
    sol_price: float,
) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO lobbies
           (lobby_id, chat_id, creator_id, max_players,
            buyin_usd, buyin_sol, sol_price_at_create,
            status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?)""",
        (lobby_id, chat_id, creator_id, max_players,
         buyin_usd, buyin_sol, sol_price,
         datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


async def get_lobby(lobby_id: str) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM lobbies WHERE lobby_id = ?", (lobby_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_lobby(lobby_id: str, **kwargs: Any) -> None:
    db = await get_db()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [lobby_id]
    await db.execute(f"UPDATE lobbies SET {sets} WHERE lobby_id = ?", vals)
    await db.commit()


async def get_active_lobbies() -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM lobbies WHERE status IN ('waiting', 'spinning')"
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────
# Lobby-player helpers
# ────────────────────────────────────────────────────────────

async def add_lobby_player(lobby_id: str, telegram_id: int) -> None:
    db = await get_db()
    await db.execute(
        """INSERT OR IGNORE INTO lobby_players (lobby_id, telegram_id, status, joined_at)
           VALUES (?, ?, 'pending', ?)""",
        (lobby_id, telegram_id, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


async def get_lobby_players(lobby_id: str) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        """SELECT lp.*, u.username, u.wallet_address
           FROM lobby_players lp
           JOIN users u ON lp.telegram_id = u.telegram_id
           WHERE lp.lobby_id = ?""",
        (lobby_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def update_player_status(lobby_id: str, telegram_id: int, status: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE lobby_players SET status = ? WHERE lobby_id = ? AND telegram_id = ?",
        (status, lobby_id, telegram_id),
    )
    await db.commit()


async def player_in_lobby(lobby_id: str, telegram_id: int) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM lobby_players WHERE lobby_id = ? AND telegram_id = ?",
        (lobby_id, telegram_id),
    )
    return (await cur.fetchone()) is not None


async def count_lobby_players(lobby_id: str) -> int:
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM lobby_players WHERE lobby_id = ?", (lobby_id,)
    )
    row = await cur.fetchone()
    return row["cnt"] if row else 0


async def player_in_any_active_lobby(telegram_id: int) -> Optional[str]:
    """Return lobby_id if the player is already in an active lobby, else None."""
    db = await get_db()
    cur = await db.execute(
        """SELECT lp.lobby_id FROM lobby_players lp
           JOIN lobbies l ON lp.lobby_id = l.lobby_id
           WHERE lp.telegram_id = ? AND l.status IN ('waiting', 'spinning', 'playing')""",
        (telegram_id,),
    )
    row = await cur.fetchone()
    return row["lobby_id"] if row else None


# ────────────────────────────────────────────────────────────
# Game helpers
# ────────────────────────────────────────────────────────────

async def create_game(
    game_id: str, lobby_id: str, chat_id: int, game_state: dict
) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO games (game_id, lobby_id, chat_id, status, game_state, started_at)
           VALUES (?, ?, ?, 'active', ?, ?)""",
        (game_id, lobby_id, chat_id, json.dumps(game_state),
         datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


async def get_game(game_id: str) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM games WHERE game_id = ?", (game_id,))
    row = await cur.fetchone()
    if row:
        d = dict(row)
        if d.get("game_state"):
            d["game_state"] = json.loads(d["game_state"])
        return d
    return None


async def get_game_by_chat(chat_id: int) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM games WHERE chat_id = ? AND status = 'active' ORDER BY started_at DESC LIMIT 1",
        (chat_id,),
    )
    row = await cur.fetchone()
    if row:
        d = dict(row)
        if d.get("game_state"):
            d["game_state"] = json.loads(d["game_state"])
        return d
    return None


async def update_game(game_id: str, **kwargs: Any) -> None:
    db = await get_db()
    if "game_state" in kwargs and isinstance(kwargs["game_state"], dict):
        kwargs["game_state"] = json.dumps(kwargs["game_state"])
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [game_id]
    await db.execute(f"UPDATE games SET {sets} WHERE game_id = ?", vals)
    await db.commit()


# ────────────────────────────────────────────────────────────
# Jackpot helpers
# ────────────────────────────────────────────────────────────

async def get_jackpot_balance() -> float:
    db = await get_db()
    cur = await db.execute("SELECT balance_usd FROM jackpot_balance WHERE id = 1")
    row = await cur.fetchone()
    return row["balance_usd"] if row else 0.0


async def update_jackpot_balance(delta_usd: float) -> float:
    """Atomically add *delta_usd* to the jackpot. Returns new balance."""
    db = await get_db()
    await db.execute(
        "UPDATE jackpot_balance SET balance_usd = balance_usd + ?, updated_at = ? WHERE id = 1",
        (delta_usd, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()
    return await get_jackpot_balance()


async def set_jackpot_balance(balance_usd: float) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE jackpot_balance SET balance_usd = ?, updated_at = ? WHERE id = 1",
        (balance_usd, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


# ────────────────────────────────────────────────────────────
# Transaction log helpers
# ────────────────────────────────────────────────────────────

async def log_transaction(
    lobby_id: str,
    telegram_id: int,
    tx_type: str,
    amount_usd: float,
    amount_sol: float,
    sol_price: float,
    tx_signature: str = "",
    notes: str = "",
) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO transaction_log
           (lobby_id, telegram_id, tx_type, amount_usd, amount_sol,
            sol_price, tx_signature, timestamp, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lobby_id, telegram_id, tx_type, amount_usd, amount_sol,
         sol_price, tx_signature, datetime.now(timezone.utc).isoformat(), notes),
    )
    await db.commit()


async def get_treasury_stats() -> dict:
    """Return internal accounting totals."""
    db = await get_db()
    stats: dict[str, float] = {}
    for tx_type in ("buyin", "payout", "creator_credit", "jackpot_credit"):
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM transaction_log WHERE tx_type = ?",
            (tx_type,),
        )
        row = await cur.fetchone()
        stats[tx_type] = row["total"] if row else 0.0
    stats["jackpot_balance"] = await get_jackpot_balance()
    return stats


# ────────────────────────────────────────────────────────────
# Processed-signature helpers (deposit dedup)
# ────────────────────────────────────────────────────────────

async def is_signature_processed(sig: str) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM processed_signatures WHERE signature = ?", (sig,)
    )
    return (await cur.fetchone()) is not None


async def mark_signature_processed(sig: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO processed_signatures (signature, processed_at) VALUES (?, ?)",
        (sig, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()
