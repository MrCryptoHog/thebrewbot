#!/usr/bin/env python3
"""
☕ TheBrewBot — The Coffee-Themed Crypto Call Tracker
A cozy crypto-café Telegram bot that lets users "brew" calls and flex gains.
Python 3.11+ | python-telegram-bot 20.x | SQLite | Pillow | DexScreener
"""

import os
import re
import io
import time
import math
import sqlite3
import logging
import textwrap
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cafebot.db")
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={}"
CACHE_TTL = 45  # seconds
DAILY_INTERVAL = 86400  # 24 h in seconds
TOP_N = 15  # cafeboard size

# Regex patterns for contract addresses
ETH_CA_RE = re.compile(r"0x[a-fA-F0-9]{40}")
SOL_CA_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BrewBot")

# ---------------------------------------------------------------------------
# In-memory DexScreener cache  {ca_lower: (timestamp, data_dict)}
# ---------------------------------------------------------------------------
_dex_cache: dict[str, tuple[float, dict]] = {}

# Temporary storage for pending calls  {message_id: {...}}
_pending_calls: dict[int, dict] = {}


# =========================================================================
# Database helpers
# =========================================================================
def _get_db() -> sqlite3.Connection:
    """Return a connection with WAL mode enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    """Create tables on first run."""
    with _get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT    NOT NULL,
                ca          TEXT    NOT NULL,
                thesis      TEXT    NOT NULL,
                call_type   TEXT    NOT NULL,
                initial_mc  REAL    NOT NULL,
                timestamp   TEXT    NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_chats (
                chat_id              INTEGER PRIMARY KEY,
                last_board_timestamp TEXT
            );
            """
        )
        conn.commit()
    logger.info("Database initialized at %s", DB_PATH)


def insert_call(
    chat_id: int,
    user_id: int,
    username: str,
    ca: str,
    thesis: str,
    call_type: str,
    initial_mc: float,
) -> None:
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO calls (chat_id, user_id, username, ca, thesis, call_type, initial_mc, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                user_id,
                username,
                ca,
                thesis,
                call_type,
                initial_mc,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def get_user_call(chat_id: int, user_id: int, ca: str) -> Optional[dict]:
    """Fetch the first call a user made for this CA in a chat."""
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM calls WHERE chat_id=? AND user_id=? AND LOWER(ca)=LOWER(?) ORDER BY id ASC LIMIT 1",
            (chat_id, user_id, ca),
        ).fetchone()
    return dict(row) if row else None


def get_chat_calls(chat_id: int) -> list[dict]:
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM calls WHERE chat_id=? ORDER BY id ASC", (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_active_chat(chat_id: int) -> None:
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO active_chats (chat_id, last_board_timestamp) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_board_timestamp=excluded.last_board_timestamp",
            (chat_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_active_chats() -> list[int]:
    with _get_db() as conn:
        rows = conn.execute("SELECT chat_id FROM active_chats").fetchall()
    return [r["chat_id"] for r in rows]


# =========================================================================
# DexScreener helpers
# =========================================================================
def get_dexscreener_data(ca: str) -> Optional[dict]:
    """
    Fetch the highest-liquidity pair for *ca* from DexScreener.
    Returns dict with keys: symbol, name, fdv, liquidity_usd, volume_24h, pair_address
    Uses a short in-memory cache to stay rate-limit friendly.
    """
    key = ca.lower()
    now = time.time()
    cached = _dex_cache.get(key)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    url = DEXSCREENER_SEARCH.format(ca)
    try:
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("DexScreener request failed for %s: %s", ca, exc)
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    # Find the pair with highest liquidity where our CA is one of the tokens
    best = None
    best_liq = -1.0
    for p in pairs:
        liq = float(p.get("liquidity", {}).get("usd") or 0)
        # Match the CA to either base or quote token
        base_addr = (p.get("baseToken") or {}).get("address", "").lower()
        quote_addr = (p.get("quoteToken") or {}).get("address", "").lower()
        if key in (base_addr, quote_addr) and liq > best_liq:
            best = p
            best_liq = liq

    if best is None:
        # Fallback: highest liquidity pair overall
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))

    # Determine which token matches our CA
    base_addr = (best.get("baseToken") or {}).get("address", "").lower()
    if base_addr == key:
        token = best["baseToken"]
    else:
        token = best.get("quoteToken") or best.get("baseToken") or {}

    result = {
        "symbol": token.get("symbol", "???"),
        "name": token.get("name", "Unknown"),
        "fdv": float(best.get("fdv") or 0),
        "liquidity_usd": float(best.get("liquidity", {}).get("usd") or 0),
        "volume_24h": float(best.get("volume", {}).get("h24") or 0),
        "pair_address": best.get("pairAddress", ""),
    }
    _dex_cache[key] = (now, result)
    return result


# =========================================================================
# Formatting helpers
# =========================================================================
def fmt_usd(n: float) -> str:
    """Pretty-print a USD value."""
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.2f}K"
    return f"${n:,.2f}"


def pct_str(x: float) -> str:
    pct = (x - 1) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def x_str(x: float) -> str:
    return f"{x:.1f}x"


# =========================================================================
# Coffee Card Image Generation (Pillow)
# =========================================================================

# Colour palette — warm coffee shop vibes
BG_TOP = (42, 28, 20)        # deep espresso
BG_BOT = (62, 42, 30)        # lighter roast
ACCENT = (210, 160, 90)      # latte gold
CREAM = (255, 248, 235)      # cream white
GREEN = (80, 210, 120)       # profit green
RED = (230, 80, 80)          # loss red
MUTED = (180, 165, 148)      # muted text
CARD_W, CARD_H = 800, 1000


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Try to load a nice system font; fall back to default."""
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    # Last resort: Pillow built-in (bitmap, not great but works)
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _draw_rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: tuple,
) -> None:
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill)


def _draw_coffee_decorations(draw: ImageDraw.ImageDraw) -> None:
    """Draw subtle coffee-themed decorations on the card."""
    # Steam wisps at top-right
    for i, offset in enumerate([(690, 40), (720, 30), (710, 55)]):
        alpha = 140 - i * 30
        col = (ACCENT[0], ACCENT[1], ACCENT[2])
        draw.ellipse(
            (offset[0], offset[1], offset[0] + 14, offset[1] + 28),
            fill=None,
            outline=col,
            width=2,
        )
    # Coffee beans (small ovals) scattered
    bean_positions = [(40, 900), (750, 880), (60, 50), (730, 500)]
    for bx, by in bean_positions:
        draw.ellipse((bx, by, bx + 18, by + 12), fill=ACCENT)
        draw.line((bx + 4, by + 2, bx + 14, by + 10), fill=BG_TOP, width=1)
    # Mug silhouette bottom-left
    draw.rounded_rectangle((30, 920, 80, 970), radius=6, fill=ACCENT)
    draw.rectangle((80, 935, 95, 958), fill=ACCENT)
    # Small ring for mug handle
    draw.arc((78, 932, 100, 960), 270, 90, fill=ACCENT, width=3)


def _fit_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Shorten text with '…' if it exceeds max_width."""
    if not text:
        return ""
    if font.getlength(text) <= max_width:
        return text
    for end in range(len(text) - 1, 0, -1):
        candidate = text[:end].rstrip() + "…"
        if font.getlength(candidate) <= max_width:
            return candidate
    return text[:10] + "…"


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width, returns list of lines."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        test = f"{current} {w}".strip()
        if font.getlength(test) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines or [""]


def generate_coffee_card_image(
    ticker: str,
    token_name: str,
    ca: str,
    username: str,
    thesis: str,
    pct_gain: float,
    x_mult: float,
    call_type: str,
    brewed_on: str,
) -> io.BytesIO:
    """
    Generate a premium coffee-themed 'Coffee Card' PNG.
    Returns a BytesIO buffer ready for Telegram send_photo.
    """
    img = Image.new("RGB", (CARD_W, CARD_H), BG_TOP)
    draw = ImageDraw.ImageDraw(img)

    # Gradient background
    for y in range(CARD_H):
        t = y / CARD_H
        r = int(BG_TOP[0] * (1 - t) + BG_BOT[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOT[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOT[2] * t)
        draw.line([(0, y), (CARD_W, y)], fill=(r, g, b))

    # Inner card panel
    _draw_rounded_rect(draw, (30, 25, CARD_W - 30, CARD_H - 25), 24, (50, 35, 26))
    # Thin accent border inside
    draw.rounded_rectangle(
        (32, 27, CARD_W - 32, CARD_H - 27), radius=23, outline=ACCENT, width=2
    )

    # Coffee decorations
    _draw_coffee_decorations(draw)

    # Fonts
    font_ticker = _load_font(52, bold=True)
    font_name = _load_font(24, bold=False)
    font_ca = _load_font(14, bold=False)
    font_label = _load_font(18, bold=True)
    font_value = _load_font(22, bold=False)
    font_xmult = _load_font(72, bold=True)
    font_pct = _load_font(40, bold=True)
    font_type_badge = _load_font(20, bold=True)
    font_thesis = _load_font(17, bold=False)
    font_brewed = _load_font(16, bold=False)
    font_header = _load_font(16, bold=True)
    font_watermark = _load_font(14, bold=False)

    y_cursor = 55

    # Header line
    header_text = "☕ Coffee Card"
    draw.text((60, y_cursor), header_text, fill=ACCENT, font=font_label)
    y_cursor += 36

    # Divider
    draw.line([(60, y_cursor), (CARD_W - 60, y_cursor)], fill=ACCENT, width=1)
    y_cursor += 18

    # Ticker (large)
    ticker_display = f"${ticker.upper()}"
    draw.text((60, y_cursor), ticker_display, fill=CREAM, font=font_ticker)
    y_cursor += 62

    # Token name
    draw.text((62, y_cursor), token_name, fill=MUTED, font=font_name)
    y_cursor += 34

    # Contract address (full, small, monospace-ish)
    ca_display = ca
    draw.text((62, y_cursor), ca_display, fill=MUTED, font=font_ca)
    y_cursor += 28

    # Divider
    draw.line([(60, y_cursor), (CARD_W - 60, y_cursor)], fill=(70, 55, 42), width=1)
    y_cursor += 16

    # Call type badge
    badge_text = "GAMBLE 🎲" if call_type == "gamble" else "ALPHA 🏆"
    badge_color = (180, 130, 60) if call_type == "gamble" else (80, 180, 130)
    badge_w = int(font_type_badge.getlength(badge_text)) + 28
    _draw_rounded_rect(
        draw, (60, y_cursor, 60 + badge_w, y_cursor + 34), 8, badge_color
    )
    draw.text(
        (74, y_cursor + 5), badge_text, fill=(255, 255, 255), font=font_type_badge
    )

    # Username next to badge
    user_display = f"@{username}" if username else "Anonymous"
    draw.text(
        (80 + badge_w, y_cursor + 7), user_display, fill=ACCENT, font=font_value
    )
    y_cursor += 50

    # X Multiplier — BIG and prominent
    x_color = GREEN if x_mult >= 1.0 else RED
    x_text = f"{x_mult:.1f}x"
    draw.text((60, y_cursor), x_text, fill=x_color, font=font_xmult)

    # % Gain next to x
    pct_color = GREEN if pct_gain >= 0 else RED
    sign = "+" if pct_gain >= 0 else ""
    pct_text = f"{sign}{pct_gain:.1f}%"
    draw.text((380, y_cursor + 20), pct_text, fill=pct_color, font=font_pct)
    y_cursor += 100

    # Divider
    draw.line([(60, y_cursor), (CARD_W - 60, y_cursor)], fill=(70, 55, 42), width=1)
    y_cursor += 16

    # Thesis section
    draw.text((60, y_cursor), "THESIS", fill=ACCENT, font=font_header)
    y_cursor += 26
    thesis_lines = _wrap_text(thesis, font_thesis, CARD_W - 140)
    max_thesis_lines = 6
    for i, line in enumerate(thesis_lines[:max_thesis_lines]):
        if i == max_thesis_lines - 1 and len(thesis_lines) > max_thesis_lines:
            line = _fit_text(line, font_thesis, CARD_W - 140)
            if not line.endswith("…"):
                line += "…"
        draw.text((62, y_cursor), line, fill=CREAM, font=font_thesis)
        y_cursor += 24
    y_cursor += 12

    # Divider
    draw.line([(60, y_cursor), (CARD_W - 60, y_cursor)], fill=(70, 55, 42), width=1)
    y_cursor += 16

    # Brewed on
    draw.text((60, y_cursor), "BREWED ON", fill=ACCENT, font=font_header)
    y_cursor += 24
    draw.text((62, y_cursor), brewed_on, fill=CREAM, font=font_value)
    y_cursor += 40

    # Watermark / branding at bottom
    wm_text = "☕ TheBrewBot — @TradingBrew"
    wm_w = font_watermark.getlength(wm_text)
    draw.text(
        ((CARD_W - wm_w) / 2, CARD_H - 60),
        wm_text,
        fill=MUTED,
        font=font_watermark,
    )

    # Export
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# =========================================================================
# Cafeboard calculation
# =========================================================================
def calculate_cafeboard(chat_id: int) -> list[dict]:
    """
    For each user in the chat, find their highest current x-multiplier
    (live from DexScreener). Returns sorted list of dicts:
    [{username, best_x, ca}, ...] descending by best_x.
    """
    calls = get_chat_calls(chat_id)
    if not calls:
        return []

    # Group by user
    user_calls: dict[int, list[dict]] = {}
    for c in calls:
        user_calls.setdefault(c["user_id"], []).append(c)

    results: list[dict] = []
    for uid, ucalls in user_calls.items():
        best_x = 0.0
        best_username = ucalls[0]["username"]
        for c in ucalls:
            dex = get_dexscreener_data(c["ca"])
            if dex and c["initial_mc"] > 0:
                cur_x = dex["fdv"] / c["initial_mc"]
                if cur_x > best_x:
                    best_x = cur_x
                    best_username = c["username"]
        if best_x > 0:
            results.append({"username": best_username, "best_x": best_x})

    results.sort(key=lambda r: r["best_x"], reverse=True)
    return results[:TOP_N]


def format_cafeboard(entries: list[dict]) -> str:
    """Render the cafeboard as a chat message."""
    if not entries:
        return "🏆 Cafeboard ☕\n\nNo brewed calls yet — be the first barista! ☕"

    lines = ["🏆 Cafeboard ☕\n"]
    for i, e in enumerate(entries, 1):
        fire = " 🔥" if i <= 3 else ""
        lines.append(f"{i}. @{e['username']} — {e['best_x']:.1f}x{fire}")
    return "\n".join(lines)


# =========================================================================
# Caption generation
# =========================================================================
def generate_caption(username: str, x_mult: float, pct_gain: float) -> str:
    """Build a warm coffee-themed caption for the Coffee Card photo."""
    if x_mult >= 10:
        compliment = (
            f"☕ Brewed to perfection! Your call just hit {x_mult:.1f}x "
            f"— you're the real alpha barista of the café! 🔥"
        )
    elif x_mult >= 5:
        compliment = (
            f"☕ Now that's a strong brew! {x_mult:.1f}x gains — "
            f"the café smells like pure alpha today! 🔥"
        )
    elif x_mult >= 2:
        compliment = (
            f"☕ A solid double shot! {x_mult:.1f}x — "
            f"your portfolio is steaming nicely! ☕"
        )
    elif x_mult >= 1:
        compliment = (
            f"☕ The brew is warming up! {x_mult:.1f}x so far — "
            f"keep that grinder going, @{username}! ☕"
        )
    else:
        compliment = (
            f"☕ Even the best baristas have off-days. {x_mult:.1f}x for now — "
            f"the next cup will be stronger, @{username}! 💪"
        )

    promo = (
        "Want your personal Coffee Card? "
        "Join the @TradingBrew community and start sharing your alpha! ☕"
    )
    return f"{compliment}\n\n{promo}"


# =========================================================================
# Contract address detection
# =========================================================================
def detect_ca(text: str) -> Optional[str]:
    """Return the first Ethereum or Solana contract address found in text."""
    m = ETH_CA_RE.search(text)
    if m:
        return m.group(0)
    # For Solana, be stricter: must be 32-44 base58 chars, surrounded by whitespace/boundaries
    # We iterate tokens to avoid false positives on normal words
    for token in text.split():
        if SOL_CA_RE.fullmatch(token) and len(token) >= 32:
            return token
    return None


# =========================================================================
# Telegram handlers
# =========================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start in private chat."""
    if update.effective_chat and update.effective_chat.type == "private":
        await update.message.reply_text(
            "☕ Welcome to TheBrewBot!\n\n"
            "Add me to a group chat to start tracking crypto calls.\n"
            "Share a contract address with a thesis (20+ chars) and I'll brew it for you!\n\n"
            "Commands:\n"
            "/pnl <CA> — Check your gains with a Coffee Card\n"
            "/cafeboard — View the group leaderboard\n"
            "/refresh — Refresh the leaderboard\n\n"
            "Brew your best alpha! ☕"
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan group messages for contract addresses."""
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    if not chat or chat.type in ("private",):
        return

    text = update.message.text
    ca = detect_ca(text)
    if not ca:
        return

    # Extract thesis (original text minus the CA)
    thesis = text.replace(ca, "").strip()
    if len(thesis) < 20:
        await update.message.reply_text(
            "To submit a call as a gamble or alpha, you have to provide a "
            "thesis/detail with the contract address you share, minimum 20 "
            "characters (contract address excluded) ☕"
        )
        return

    # Fetch DexScreener data
    dex = get_dexscreener_data(ca)
    if not dex:
        await update.message.reply_text(
            "☕ Couldn't find that token on DexScreener. "
            "Double-check the contract address and try again!"
        )
        return

    if dex["fdv"] <= 0:
        await update.message.reply_text(
            "☕ Token found but market cap data isn't available yet. Try again later!"
        )
        return

    # Build reply
    info_text = (
        f"☕ <b>{dex['name']}</b> (${dex['symbol']})\n\n"
        f"💰 Market Cap (FDV): <b>{fmt_usd(dex['fdv'])}</b>\n"
        f"💧 Liquidity: <b>{fmt_usd(dex['liquidity_usd'])}</b>\n"
        f"📊 24h Volume: <b>{fmt_usd(dex['volume_24h'])}</b>\n\n"
        f"<i>Pick your brew below ↓</i>"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Gamble 🎲", callback_data=f"call:gamble:{ca}"),
                InlineKeyboardButton("Alpha 🏆", callback_data=f"call:alpha:{ca}"),
            ]
        ]
    )

    sent = await update.message.reply_text(
        info_text, parse_mode="HTML", reply_markup=keyboard
    )

    # Store pending call data keyed by the bot's reply message ID
    _pending_calls[sent.message_id] = {
        "ca": ca,
        "thesis": text,  # full original message text
        "initial_mc": dex["fdv"],
        "user_id": update.message.from_user.id,
        "username": update.message.from_user.username or update.message.from_user.first_name or "Anon",
        "chat_id": chat.id,
        "dex": dex,
    }


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Gamble / Alpha button presses."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "call":
        return

    call_type = parts[1]  # "gamble" or "alpha"
    ca = parts[2]

    msg_id = query.message.message_id
    pending = _pending_calls.get(msg_id)
    if not pending:
        await query.answer("☕ This call has already been brewed or expired!", show_alert=True)
        return

    # Only the original caller can submit
    clicker = query.from_user
    if clicker.id != pending["user_id"]:
        await query.answer("☕ Only the original caller can brew this one!", show_alert=True)
        return

    # Check for duplicate
    existing = get_user_call(pending["chat_id"], pending["user_id"], ca)
    if existing:
        await query.answer("☕ You've already brewed a call for this token!", show_alert=True)
        return

    # Insert into DB
    insert_call(
        chat_id=pending["chat_id"],
        user_id=pending["user_id"],
        username=pending["username"],
        ca=ca,
        thesis=pending["thesis"],
        call_type=call_type,
        initial_mc=pending["initial_mc"],
    )

    # Clean up pending
    del _pending_calls[msg_id]

    type_label = "Gamble 🎲" if call_type == "gamble" else "Alpha 🏆"
    dex = pending["dex"]
    confirm_text = (
        f"☕ <b>{dex['name']}</b> (${dex['symbol']})\n\n"
        f"💰 Market Cap (FDV): <b>{fmt_usd(dex['fdv'])}</b>\n"
        f"💧 Liquidity: <b>{fmt_usd(dex['liquidity_usd'])}</b>\n"
        f"📊 24h Volume: <b>{fmt_usd(dex['volume_24h'])}</b>\n\n"
        f"✅ Call submitted as <b>{type_label}</b>! ☕ Initial MC locked in."
    )

    await query.edit_message_text(text=confirm_text, parse_mode="HTML")
    logger.info(
        "Call submitted: user=%s type=%s ca=%s mc=%s",
        pending["username"],
        call_type,
        ca,
        pending["initial_mc"],
    )


async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pnl <contract_address>."""
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("☕ Use /pnl in a group chat!")
        return

    text = update.message.text or ""
    ca = detect_ca(text)
    if not ca:
        await update.message.reply_text(
            "☕ Please provide a contract address: /pnl <contract_address>"
        )
        return

    user = update.message.from_user
    call = get_user_call(chat.id, user.id, ca)
    if not call:
        await update.message.reply_text(
            "☕ No brewed call found for that token in this chat. "
            "Share a CA with your thesis first!"
        )
        return

    # Fetch current data
    dex = get_dexscreener_data(ca)
    if not dex:
        await update.message.reply_text(
            "☕ Couldn't fetch current data from DexScreener. Try again shortly!"
        )
        return

    current_mc = dex["fdv"]
    initial_mc = call["initial_mc"]
    if initial_mc <= 0:
        await update.message.reply_text("☕ Initial market cap was zero — can't calculate PnL!")
        return

    x_mult = current_mc / initial_mc
    pct_gain = (x_mult - 1) * 100

    # Parse timestamp for "Brewed on"
    try:
        ts = datetime.fromisoformat(call["timestamp"])
        brewed_on = ts.strftime("%b %d, %Y at %H:%M UTC")
    except Exception:
        brewed_on = call["timestamp"]

    username = call["username"]

    # Generate Coffee Card
    card_buf = generate_coffee_card_image(
        ticker=dex["symbol"],
        token_name=dex["name"],
        ca=ca,
        username=username,
        thesis=call["thesis"],
        pct_gain=pct_gain,
        x_mult=x_mult,
        call_type=call["call_type"],
        brewed_on=brewed_on,
    )

    caption = generate_caption(username, x_mult, pct_gain)

    await update.message.reply_photo(photo=card_buf, caption=caption)
    logger.info("PnL card sent for user=%s ca=%s x=%.1f", username, ca, x_mult)


async def cafeboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cafeboard — show the leaderboard."""
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("☕ Use /cafeboard in a group chat!")
        return

    # Register chat for daily auto-posts
    upsert_active_chat(chat.id)

    await update.message.reply_text("☕ Brewing the Cafeboard… hang tight!")

    entries = calculate_cafeboard(chat.id)
    board_text = format_cafeboard(entries)
    await update.message.reply_text(board_text)
    logger.info("Cafeboard posted for chat %s (%d entries)", chat.id, len(entries))


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /refresh — recalculate and post the cafeboard."""
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("☕ Use /refresh in a group chat!")
        return

    upsert_active_chat(chat.id)

    # Clear cache to force fresh data
    _dex_cache.clear()

    await update.message.reply_text("☕ Refreshing the Cafeboard with fresh beans…")

    entries = calculate_cafeboard(chat.id)
    board_text = format_cafeboard(entries)
    await update.message.reply_text(board_text)
    logger.info("Cafeboard refreshed for chat %s (%d entries)", chat.id, len(entries))


# =========================================================================
# Daily auto-post job
# =========================================================================
async def daily_cafeboard_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-post the cafeboard to all active groups every 24 hours."""
    logger.info("Running daily cafeboard auto-post…")
    active = get_active_chats()
    for chat_id in active:
        try:
            # Clear cache per-chat to get fresh data
            _dex_cache.clear()
            entries = calculate_cafeboard(chat_id)
            board_text = "☕ Daily Cafeboard Update!\n\n" + format_cafeboard(entries)
            await context.bot.send_message(chat_id=chat_id, text=board_text)
            upsert_active_chat(chat_id)
            logger.info("Daily board posted to chat %s", chat_id)
        except Exception as exc:
            logger.warning("Failed to post daily board to %s: %s", chat_id, exc)


# =========================================================================
# Main
# =========================================================================
def main() -> None:
    """Start TheBrewBot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set — create a .env file with BOT_TOKEN=your_token")
        return

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("pnl", pnl_command))
    app.add_handler(CommandHandler("cafeboard", cafeboard_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # Message handler — must be last to avoid swallowing commands
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handle_message)
    )

    # Schedule daily cafeboard job (first run 24h after startup)
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(
            daily_cafeboard_job,
            interval=DAILY_INTERVAL,
            first=DAILY_INTERVAL,
            name="daily_cafeboard",
        )
        logger.info("Daily cafeboard job scheduled (every %ds)", DAILY_INTERVAL)
    else:
        logger.warning("JobQueue not available — daily auto-post disabled")

    logger.info("☕ TheBrewBot is starting up…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
